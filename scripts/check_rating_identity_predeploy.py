#!/usr/bin/env python3
"""Pre-deploy rating-identity guard (P5, 2026-10-05).

Two regressions were discovered only through runtime daemon crashes:
043919c9 changed a file inside ``evaluation_data_identity.SEMANTIC_PATHS``
and af913b91 changed the pinned ``POK_DAEMON_PAIRS`` knob — both make the
rating daemon fail closed at startup with ``EvaluationDataIdentityError``
(``rating daemon runtime profile changed; archive and restart ratings`` /
``authoritative rating evaluator identity changed; archive and restart
ratings``).  Protection was operator memory only.

This read-only preflight compares the deployment baseline against the
to-be-deployed revision:

1. any content change inside the SEMANTIC_PATHS file set, or
2. any change of ``POK_DAEMON_PAIRS`` in ``deploy/tencent-cloud/env.runtime``
   (workspace file wins over the head revision, because deployed env
   edits are often uncommitted),

and exits non-zero with the explicit archive-and-reset instruction.

Usage:
    python scripts/check_rating_identity_predeploy.py \
        [--baseline origin/tencent-cloud-runtime] [--head HEAD] \
        [--env-runtime deploy/tencent-cloud/env.runtime] \
        [--semantic-paths-file <one relative path per line>]

``POK_DAEMON_WORKERS`` is deliberately NOT guarded: it is not part of the
rating identity runtime_profile (operator-verified 2026-10-05 — workers
scaled 1→3 with pairs pinned and the daemon kept its identity).
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT_PLACEHOLDER = Path(__file__).resolve().parents[1]
DEFAULT_HEAD = "HEAD"
DEFAULT_ENV_RUNTIME = "deploy/tencent-cloud/env.runtime"

EXIT_CLEAN = 0
EXIT_IDENTITY_CHANGE = 2
EXIT_TOOLING_ERROR = 3

INSTRUCTION = (
    "需归档重置评分（evaluation_data_identity）并按文档执行，"
    "否则 daemon 将 fail-closed"
)


def _repo_root() -> Path:
    """The git checkout the operator invoked the script from.

    Prefers ``git rev-parse --show-toplevel`` of the current directory so a
    scratch checkout works; falls back to the script's own repository.
    """

    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=os.getcwd(),
            capture_output=True,
            text=True,
        )
        if completed.returncode == 0 and completed.stdout.strip():
            return Path(completed.stdout.strip()).resolve()
    except OSError:
        pass
    return REPO_ROOT_PLACEHOLDER


def resolve_default_baseline() -> str:
    """Tracking branch of the current checkout (HEAD when detached/none)."""

    try:
        completed = subprocess.run(
            [
                "git",
                "rev-parse",
                "--abbrev-ref",
                "--symbolic-full-name",
                "@{upstream}",
            ],
            cwd=os.getcwd(),
            capture_output=True,
            text=True,
        )
        if completed.returncode == 0 and completed.stdout.strip():
            return completed.stdout.strip()
    except OSError:
        pass
    return "HEAD"


def _git(*args: str, cwd: Path | None = None) -> str:
    workdir = cwd or _repo_root()
    completed = subprocess.run(
        ("git", *args), cwd=str(workdir), capture_output=True, text=True
    )
    if completed.returncode != 0:
        raise SystemExit(
            f"git {' '.join(args)} 失败: {completed.stderr.strip()[:400]}"
        )
    return completed.stdout


def load_semantic_paths(override_file: str | None) -> tuple[str, ...]:
    if override_file:
        lines = [
            line.strip()
            for line in Path(override_file).read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        return tuple(lines)
    core_dir = _repo_root() / "web" / "core"
    sys.path.insert(0, str(core_dir))
    try:
        from evaluation_data_identity import SEMANTIC_PATHS  # type: ignore
    except Exception as exc:  # pragma: no cover - real-repo path
        raise SystemExit(
            f"无法导入 evaluation_data_identity.SEMANTIC_PATHS: {exc}"
        ) from exc
    return tuple(SEMANTIC_PATHS)


def changed_files(baseline: str, head: str) -> set[str]:
    """Committed + staged + unstaged tracked-file changes vs the baseline.

    B3 (red-team, 2026-10-05): the committed-only diff
    (``git diff --name-only baseline head``) is empty by construction in
    both documented call modes — after push (tracking == HEAD) and in the
    ExecStartPre ``--baseline HEAD --head HEAD`` mode — so an uncommitted
    SEMANTIC_PATHS edit sailed through with rc=0 (reproduced against the
    real worktree).  ``git diff --name-only <baseline>`` compares the
    baseline against the WORKING TREE (staged and unstaged, tracked files
    only), which is exactly what a deploy would actually ship.
    """
    committed = _git("diff", "--name-only", baseline, head)
    working_tree = _git("diff", "--name-only", baseline)
    return {line.strip() for line in committed.splitlines() if line.strip()} | {
        line.strip() for line in working_tree.splitlines() if line.strip()
    }


def read_rev_file(rev: str, rel_path: str) -> str | None:
    completed = subprocess.run(
        ["git", "show", f"{rev}:{rel_path}"],
        cwd=str(_repo_root()),
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        return None
    return completed.stdout


def parse_env_value(text: str | None, key: str) -> str | None:
    if not text:
        return None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith(f"{key}="):
            return stripped.split("=", 1)[1].strip()
    return None


def evaluate(
    semantic_paths: tuple[str, ...],
    changed: set[str],
    baseline_pairs: str | None,
    head_pairs: str | None,
) -> list[tuple[str, list[str]]]:
    problems: list[tuple[str, list[str]]] = []
    semantic_hits = sorted(path for path in semantic_paths if path in changed)
    if semantic_hits:
        problems.append(("SEMANTIC_PATHS 文件内容变化", semantic_hits))
    if baseline_pairs != head_pairs:
        problems.append(
            (
                "env.runtime 评分身份参数变化",
                [f"POK_DAEMON_PAIRS {baseline_pairs!r} -> {head_pairs!r}"],
            )
        )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "部署预检：对比基线与待部署修订的评分身份（SEMANTIC_PATHS 文件集"
            "内容哈希 + env.runtime 的 POK_DAEMON_PAIRS），命中即提示需归档"
            "重置评分并按非零码退出。"
        )
    )
    parser.add_argument(
        "--baseline",
        default=None,
        help=(
            "部署基线 git 修订（默认：当前分支的跟踪分支，"
            "如 origin/tencent-cloud-runtime；无跟踪分支时回退 HEAD）"
        ),
    )
    parser.add_argument(
        "--head",
        default=DEFAULT_HEAD,
        help=f"待部署 git 修订（默认 {DEFAULT_HEAD}）",
    )
    parser.add_argument(
        "--env-runtime",
        default=DEFAULT_ENV_RUNTIME,
        help=f"待部署 env.runtime 路径（工作区文件优先；默认 {DEFAULT_ENV_RUNTIME}）",
    )
    parser.add_argument(
        "--semantic-paths-file",
        default=None,
        help=(
            "可选：覆盖 SEMANTIC_PATHS 列表（每行一个仓库相对路径）；"
            "默认从 web/core/evaluation_data_identity 导入"
        ),
    )
    args = parser.parse_args(argv)
    baseline = args.baseline or resolve_default_baseline()

    try:
        semantic_paths = load_semantic_paths(args.semantic_paths_file)
        changed = changed_files(baseline, args.head)
        baseline_env = read_rev_file(baseline, args.env_runtime)
        workspace_env_path = _repo_root() / args.env_runtime
        head_env = (
            workspace_env_path.read_text(encoding="utf-8")
            if workspace_env_path.is_file()
            else read_rev_file(args.head, args.env_runtime)
        )
    except SystemExit as exc:
        print(f"[预检工具错误] {exc}", file=sys.stderr)
        return EXIT_TOOLING_ERROR

    baseline_pairs = parse_env_value(baseline_env, "POK_DAEMON_PAIRS")
    head_pairs = parse_env_value(head_env, "POK_DAEMON_PAIRS")
    problems = evaluate(semantic_paths, changed, baseline_pairs, head_pairs)

    print(f"预检: baseline={baseline} head={args.head}")
    print(f"SEMANTIC_PATHS 文件数: {len(semantic_paths)}; 变更文件数: {len(changed)}")
    if not problems:
        print("结果: 评分身份无变化，可直接部署。")
        return EXIT_CLEAN

    print()
    for title, details in problems:
        print(f"[命中] {title}:")
        for detail in details:
            print(f"  - {detail}")
    print()
    print(f"!! {INSTRUCTION}")
    print(
        "参考: scripts/evaluation_data_identity.py 的归档流程与 "
        "AGENTS.md『评分身份钉死』章节；归档重置会清空当前 ratings/H2H/"
        "样本历史，属于操作员决策。"
    )
    return EXIT_IDENTITY_CHANGE


if __name__ == "__main__":
    sys.exit(main())
