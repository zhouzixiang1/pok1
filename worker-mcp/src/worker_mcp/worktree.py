"""Idempotent, owner-marked detached Git worktrees and diff verification."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import subprocess
from typing import Any

from .config import WorkerConfig
from .permissions import PathPolicy
from .schemas import TaskEnvelope


OWNER_MARKER_SCHEMA = "pok-worker-mcp-owner-v1"
OWNER_MARKER_NAME = "pok-worker-mcp-owner.json"


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

    def changed_files(self, worktree: Path) -> tuple[str, ...]:
        raw = subprocess.run(
            self._git_argv(
                worktree,
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
            ),
            check=False,
            capture_output=True,
            timeout=120,
            env={"PATH": os.environ.get("PATH", ""), "LC_ALL": "C.UTF-8"},
        )
        if raw.returncode != 0:
            raise WorktreeError("unable to inspect worktree status")
        entries = raw.stdout.decode("utf-8", "surrogateescape").split("\0")
        changed: list[str] = []
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
            if "R" in code or "C" in code:
                if index < len(entries) and entries[index]:
                    changed.append(entries[index])
                    index += 1
        return tuple(sorted(set(changed)))

    def snapshot(self, worktree: Path, *, max_bytes: int = 2 * 1024 * 1024) -> WorktreeSnapshot:
        marker = self.owner_marker(worktree)
        head = self._git(worktree, "rev-parse", "HEAD").stdout.strip()
        changed = self.changed_files(worktree)
        tracked = subprocess.run(
            self._git_argv(
                worktree,
                "diff",
                "--binary",
                "--no-ext-diff",
                "HEAD",
                "--",
            ),
            check=False,
            capture_output=True,
            timeout=120,
            env={"PATH": os.environ.get("PATH", ""), "LC_ALL": "C.UTF-8"},
        )
        if tracked.returncode != 0:
            raise WorktreeError("unable to collect tracked diff")
        chunks = [tracked.stdout]
        untracked = self._git(
            worktree,
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
        ).stdout.split("\0")
        for item in sorted(path for path in untracked if path):
            candidate = (worktree / item).resolve(strict=False)
            try:
                candidate.relative_to(worktree.resolve())
            except ValueError as exc:
                raise WorktreeError("untracked diff path escaped worktree") from exc
            result = subprocess.run(
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
                check=False,
                capture_output=True,
                timeout=120,
                env={"PATH": os.environ.get("PATH", ""), "LC_ALL": "C.UTF-8"},
            )
            if result.returncode not in {0, 1}:
                raise WorktreeError(f"unable to collect untracked diff for {item}")
            chunks.append(result.stdout)
        payload = b"".join(chunks)
        truncated = len(payload) > max_bytes
        if truncated:
            payload = payload[:max_bytes] + b"\n[worker-mcp diff truncated]\n"
        if marker.get("task_id") is None:
            raise WorktreeError("owner marker lost task identity")
        return WorktreeSnapshot(
            path=worktree,
            head=head,
            changed_files=changed,
            diff=payload.decode("utf-8", "replace"),
            truncated=truncated,
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
        worktree_path: str,
    ) -> None:
        repo = self.canonical_repo(repository)
        worktree = Path(worktree_path).resolve()
        try:
            worktree.relative_to(self.root)
        except ValueError as exc:
            raise WorktreeError("cleanup target is outside configured worktree_root") from exc
        marker = self.owner_marker(worktree)
        if marker.get("task_id") != task_id or marker.get("repository") != str(repo):
            raise WorktreeError("cleanup owner marker does not match durable task")
        snapshot = self.snapshot(worktree)
        if snapshot.dirty:
            raise WorktreeDirty("refusing to remove a dirty Worker worktree")
        self._git(repo, "worktree", "unlock", str(worktree))
        self._git(repo, "worktree", "remove", str(worktree))
