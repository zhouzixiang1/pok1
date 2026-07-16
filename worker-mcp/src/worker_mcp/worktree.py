"""Idempotent, owner-marked detached Git worktrees and diff verification."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import selectors
import subprocess
import time
from typing import Any

from .config import WorkerConfig
from .permissions import PathPolicy
from .schemas import TaskEnvelope


OWNER_MARKER_SCHEMA = "pok-worker-mcp-owner-v1"
OWNER_MARKER_NAME = "pok-worker-mcp-owner.json"
RESOURCE_LIMIT_SENTINEL = "<worker-mcp-resource-limit>"


class WorktreeError(RuntimeError):
    retryable = False


class WorktreeDirty(WorktreeError):
    pass


@dataclass(frozen=True)
class WorktreeSnapshot:
    path: Path
    head: str
    changed_files: tuple[str, ...]
    diff: str
    truncated: bool = False
    ignored_files: tuple[str, ...] = ()

    @property
    def dirty(self) -> bool:
        return bool(self.changed_files)


class WorktreeManager:
    def __init__(self, config: WorkerConfig):
        self.config = config
        self.root = config.worktree_root.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    @staticmethod
    def _git_argv(repo_or_worktree: Path, *args: str) -> list[str]:
        return [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.pager=cat",
            "-C",
            str(repo_or_worktree),
            *args,
        ]

    @staticmethod
    def _git(
        repo_or_worktree: Path,
        *args: str,
        allowed_returncodes: tuple[int, ...] = (0,),
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            WorktreeManager._git_argv(repo_or_worktree, *args),
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
            env={
                "PATH": os.environ.get("PATH", ""),
                "HOME": str(Path.home()),
                "LC_ALL": "C.UTF-8",
                "GIT_TERMINAL_PROMPT": "0",
            },
        )
        if result.returncode not in allowed_returncodes:
            stderr = (result.stderr or result.stdout).strip()[:2000]
            raise WorktreeError(f"git command failed ({result.returncode}): {stderr}")
        return result

    @staticmethod
    def _bounded_output(
        argv: list[str],
        *,
        max_stdout: int,
        allowed_returncodes: tuple[int, ...] = (0,),
        timeout: float = 120,
    ) -> tuple[bytes, bool]:
        """Capture a subprocess without ever buffering beyond ``max_stdout``.

        Git diff/status output is repository-controlled.  Reading through
        non-blocking pipes lets us terminate the producer as soon as the
        evidence budget is exceeded instead of allocating the complete output
        and truncating it afterwards.
        """

        process = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={"PATH": os.environ.get("PATH", ""), "LC_ALL": "C.UTF-8"},
        )
        assert process.stdout is not None and process.stderr is not None
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        stdout = bytearray()
        stderr = bytearray()
        truncated = False
        deadline = time.monotonic() + timeout
        try:
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    process.kill()
                    raise WorktreeError("git evidence command timed out")
                events = selector.select(timeout=min(1.0, remaining))
                for key, _ in events:
                    chunk = os.read(key.fileobj.fileno(), 65_536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    if key.data == "stderr":
                        if len(stderr) < 8192:
                            stderr.extend(chunk[: 8192 - len(stderr)])
                        continue
                    available = max_stdout - len(stdout)
                    if len(chunk) > available:
                        stdout.extend(chunk[: max(0, available)])
                        truncated = True
                        process.kill()
                        break
                    stdout.extend(chunk)
                if truncated:
                    break
            returncode = process.wait(timeout=max(0.1, deadline - time.monotonic()))
        finally:
            selector.close()
            for stream in (process.stdout, process.stderr):
                stream.close()
            if process.poll() is None:
                process.kill()
                process.wait()
        if not truncated and returncode not in allowed_returncodes:
            detail = stderr.decode("utf-8", "replace").strip()[:2000]
            raise WorktreeError(
                f"git evidence command failed ({returncode}): {detail}"
            )
        return bytes(stdout), truncated

    def canonical_repo(self, value: str | Path) -> Path:
        requested = Path(value).expanduser().resolve()
        allowed = {path.resolve() for path in self.config.allowed_repositories}
        if requested not in allowed:
            raise WorktreeError("repository is not in allowed_repositories")
        top = Path(
            self._git(requested, "rev-parse", "--show-toplevel").stdout.strip()
        ).resolve()
        if top != requested:
            raise WorktreeError("repository path must be the Git toplevel")
        return top

    def resolve_base(self, repo: Path, value: str) -> str:
        if value.startswith("-"):
            raise WorktreeError("base commit cannot begin with an option prefix")
        result = self._git(
            repo,
            "rev-parse",
            "--verify",
            "--end-of-options",
            f"{value}^{{commit}}",
        )
        commit = result.stdout.strip()
        if len(commit) != 40 or any(ch not in "0123456789abcdef" for ch in commit):
            raise WorktreeError("base commit did not resolve to a full object id")
        return commit

    def validate_request(self, request: TaskEnvelope) -> TaskEnvelope:
        repo = self.canonical_repo(request.repo)
        commit = self.resolve_base(repo, request.base_commit)
        return request.model_copy(update={"repo": str(repo), "base_commit": commit})

    def path_for(self, repo: Path, commit: str, task_id: str) -> Path:
        repo_key = hashlib.sha256(str(repo).encode("utf-8")).hexdigest()[:16]
        return self.root / repo_key / commit[:12] / task_id

    def _marker_path(self, worktree: Path) -> Path:
        git_dir_text = self._git(worktree, "rev-parse", "--git-dir").stdout.strip()
        git_dir = Path(git_dir_text)
        if not git_dir.is_absolute():
            git_dir = (worktree / git_dir).resolve()
        return git_dir / OWNER_MARKER_NAME

    def _write_marker(
        self,
        worktree: Path,
        *,
        task_id: str,
        repo: Path,
        base_commit: str,
    ) -> None:
        marker = self._marker_path(worktree)
        marker.write_text(
            json.dumps(
                {
                    "schema": OWNER_MARKER_SCHEMA,
                    "task_id": task_id,
                    "repository": str(repo),
                    "worktree_path": str(worktree),
                    "base_commit": base_commit,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        marker.chmod(0o600)

    def owner_marker(self, worktree: Path) -> dict[str, Any]:
        try:
            marker_path = self._marker_path(worktree)
            payload = json.loads(marker_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, WorktreeError) as exc:
            raise WorktreeError("worktree owner marker is missing or corrupt") from exc
        if not isinstance(payload, dict) or payload.get("schema") != OWNER_MARKER_SCHEMA:
            raise WorktreeError("worktree owner marker has the wrong schema")
        if Path(str(payload.get("worktree_path", ""))).resolve() != worktree.resolve():
            raise WorktreeError("worktree owner marker path mismatch")
        return payload

    def prepare(self, request: TaskEnvelope, task_id: str) -> Path:
        repo = self.canonical_repo(request.repo)
        commit = self.resolve_base(repo, request.base_commit)
        worktree = self.path_for(repo, commit, task_id)
        try:
            worktree.resolve().relative_to(self.root)
        except ValueError as exc:
            raise WorktreeError("computed worktree escaped configured root") from exc
        if worktree.exists():
            marker = self.owner_marker(worktree)
            if marker.get("task_id") != task_id or marker.get("base_commit") != commit:
                raise WorktreeError("existing worktree belongs to a different task")
            head = self._git(worktree, "rev-parse", "HEAD").stdout.strip()
            if head != commit:
                raise WorktreeError("existing worktree HEAD does not match task base")
            return worktree

        worktree.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._git(repo, "worktree", "add", "--detach", str(worktree), commit)
        try:
            self._git(repo, "worktree", "lock", "--reason", OWNER_MARKER_SCHEMA, str(worktree))
            self._write_marker(
                worktree,
                task_id=task_id,
                repo=repo,
                base_commit=commit,
            )
        except Exception:
            try:
                self._git(repo, "worktree", "unlock", str(worktree))
                self._git(repo, "worktree", "remove", str(worktree))
            except WorktreeError:
                pass
            raise
        return worktree

    def _worktree_status(
        self, worktree: Path
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        raw, truncated = self._bounded_output(
            self._git_argv(
                worktree,
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
                "--ignored=matching",
            ),
            max_stdout=max(65_536, self.config.limits.max_changed_files * 8192),
        )
        if truncated:
            sentinel = (RESOURCE_LIMIT_SENTINEL,)
            return sentinel, sentinel
        try:
            entries = raw.decode("utf-8", "strict").split("\0")
        except UnicodeDecodeError as exc:
            raise WorktreeError(
                "worktree contains a non-UTF-8 path; evidence is unverifiable"
            ) from exc
        changed: list[str] = []
        ignored: list[str] = []
        index = 0
        while index < len(entries):
            entry = entries[index]
            index += 1
            if not entry:
                continue
            if len(entry) < 4:
                raise WorktreeError("malformed git status record")
            code = entry[:2]
            path = entry[3:]
            changed.append(path)
            if code == "!!":
                ignored.append(path)
            if "R" in code or "C" in code:
                if index < len(entries) and entries[index]:
                    changed.append(entries[index])
                    index += 1
            if len(set(changed)) > self.config.limits.max_changed_files:
                sentinel = (RESOURCE_LIMIT_SENTINEL,)
                return sentinel, sentinel
        return tuple(sorted(set(changed))), tuple(sorted(set(ignored)))

    def changed_files(self, worktree: Path) -> tuple[str, ...]:
        changed, _ = self._worktree_status(worktree)
        return changed

    def snapshot(self, worktree: Path, *, max_bytes: int | None = None) -> WorktreeSnapshot:
        marker = self.owner_marker(worktree)
        head = self._git(worktree, "rev-parse", "HEAD").stdout.strip()
        changed, ignored = self._worktree_status(worktree)
        limit = min(
            max_bytes if max_bytes is not None else self.config.limits.max_diff_bytes,
            self.config.limits.max_diff_bytes,
        )
        if changed == (RESOURCE_LIMIT_SENTINEL,):
            return WorktreeSnapshot(
                path=worktree,
                head=head,
                changed_files=changed,
                diff="[worker-mcp changed-file evidence exceeded resource limits]\n",
                truncated=True,
                ignored_files=ignored,
            )
        for item in changed:
            candidate = (worktree / item.rstrip("/")).resolve(strict=False)
            try:
                candidate.relative_to(worktree.resolve())
            except ValueError as exc:
                raise WorktreeError("changed path escaped worktree") from exc
            if candidate.is_file() and candidate.stat().st_size > self.config.limits.max_changed_file_bytes:
                return WorktreeSnapshot(
                    path=worktree,
                    head=head,
                    changed_files=changed,
                    diff=(
                        "[worker-mcp changed file exceeded per-file resource limit: "
                        f"{item}]\n"
                    ),
                    truncated=True,
                    ignored_files=ignored,
                )
        tracked, truncated = self._bounded_output(
            self._git_argv(
                worktree,
                "diff",
                "--binary",
                "--no-ext-diff",
                "HEAD",
                "--",
            ),
            max_stdout=limit,
        )
        chunks = [tracked]
        if truncated:
            chunks.append(b"\n[worker-mcp diff truncated at resource limit]\n")
        untracked = self._git(
            worktree,
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
        ).stdout.split("\0")
        for item in sorted(path for path in untracked if path):
            if truncated:
                break
            candidate = (worktree / item).resolve(strict=False)
            try:
                candidate.relative_to(worktree.resolve())
            except ValueError as exc:
                raise WorktreeError("untracked diff path escaped worktree") from exc
            remaining = max(0, limit - sum(len(chunk) for chunk in chunks))
            result, item_truncated = self._bounded_output(
                self._git_argv(
                    worktree,
                    "diff",
                    "--no-index",
                    "--no-ext-diff",
                    "--binary",
                    "--",
                    "/dev/null",
                    item,
                ),
                max_stdout=remaining,
                allowed_returncodes=(0, 1),
            )
            chunks.append(result)
            if item_truncated:
                truncated = True
                chunks.append(b"\n[worker-mcp diff truncated at resource limit]\n")
        payload = b"".join(chunks)
        if marker.get("task_id") is None:
            raise WorktreeError("owner marker lost task identity")
        return WorktreeSnapshot(
            path=worktree,
            head=head,
            changed_files=changed,
            diff=payload.decode("utf-8", "replace"),
            truncated=truncated,
            ignored_files=ignored,
        )

    def verify_changed_scope(
        self,
        request: TaskEnvelope,
        snapshot: WorktreeSnapshot,
    ) -> list[str]:
        policy = PathPolicy(
            snapshot.path,
            tuple(request.allowed_paths),
            tuple(sorted(set(request.forbidden_paths + self.config.mandatory_forbidden_paths))),
            request.execution.read_only,
        )
        violations: list[str] = []
        for path in snapshot.changed_files:
            decision = policy.check(path, write=True)
            if not decision.allowed:
                violations.append(f"{path}: {decision.reason}")
        return violations

    def cleanup_owned_clean(
        self,
        *,
        task_id: str,
        repository: str,
        base_commit: str,
        worktree_path: str,
    ) -> None:
        repo = self.canonical_repo(repository)
        commit = self.resolve_base(repo, base_commit)
        worktree = Path(worktree_path).resolve()
        try:
            worktree.relative_to(self.root)
        except ValueError as exc:
            raise WorktreeError("cleanup target is outside configured worktree_root") from exc
        expected = self.path_for(repo, commit, task_id).resolve()
        if worktree != expected:
            raise WorktreeError(
                "cleanup target does not match repository/base/task identity"
            )
        marker = self.owner_marker(worktree)
        if (
            marker.get("task_id") != task_id
            or marker.get("repository") != str(repo)
            or marker.get("base_commit") != commit
        ):
            raise WorktreeError("cleanup owner marker does not match durable task")
        snapshot = self.snapshot(worktree)
        if snapshot.head != commit:
            raise WorktreeError("cleanup worktree HEAD does not match durable base")
        if snapshot.dirty:
            raise WorktreeDirty("refusing to remove a dirty Worker worktree")
        self._git(repo, "worktree", "unlock", str(worktree))
        self._git(repo, "worktree", "remove", str(worktree))
