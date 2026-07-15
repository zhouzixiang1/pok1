import asyncio

import pytest


def _decision(output):
    return (output.get("hookSpecificOutput") or {}).get("permissionDecision")


def _reason(output):
    return (output.get("hookSpecificOutput") or {}).get(
        "permissionDecisionReason", ""
    )


def _invoke(handler, tool_name, tool_input):
    return asyncio.run(
        handler(
            {"tool_name": tool_name, "tool_input": tool_input},
            "read-scope-test",
            {},
        )
    )


@pytest.fixture
def read_scope_tree(tmp_path, monkeypatch):
    import llm_query

    root = tmp_path / "pok"
    target = root / "bots" / "national_v143"
    source = root / "bots" / "national_v144"
    snapshot = root / "web" / "core" / "results" / "v145" / "evidence_snapshot"
    live_results = root / "web" / "core" / "results"
    for directory in (target, source, snapshot):
        directory.mkdir(parents=True, exist_ok=True)
    for directory, value in ((target, 143), (source, 144)):
        (directory / "policy.py").write_text(
            f"def decide():\n    return {value}\n",
            encoding="utf-8",
        )
    (snapshot / "selection_snapshot.json").write_text("{}\n", encoding="utf-8")
    (live_results / "head_to_head.json").write_text("{}\n", encoding="utf-8")
    outside = root / "web" / "core" / "llm_query.py"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_text("# system source\n", encoding="utf-8")

    monkeypatch.setattr(llm_query, "_project_root_for_guard", lambda: root.resolve())
    return {
        "module": llm_query,
        "root": root,
        "target": target,
        "source": source,
        "snapshot": snapshot,
        "live_results": live_results,
        "outside": outside,
    }


def test_actual_read_hook_accepts_only_exact_role_roots(read_scope_tree):
    tree = read_scope_tree
    llm_query = tree["module"]
    hooks = llm_query._make_subagent_read_scope_guard(
        "MASTER (Try 1)",
        {
            "dirs": [tree["target"], tree["source"], tree["snapshot"]],
        },
    )
    handler = hooks["PreToolUse"][0].hooks[0]

    for key, path in (
        ("file_path", tree["target"] / "policy.py"),
        ("path", tree["source"] / "policy.py"),
        ("file_path", tree["snapshot"] / "selection_snapshot.json"),
    ):
        assert _decision(_invoke(handler, "Read", {key: str(path)})) is None

    denied = {
        "historical_bot": tree["root"] / "bots" / "national_v142" / "policy.py",
        "other_bot": tree["root"] / "bots" / "national_v146" / "policy.py",
        "git_log": tree["root"] / ".git" / "logs" / "HEAD",
        "archived_tree": tree["root"] / "archive" / "bots" / "policy.py",
        "live_results": tree["live_results"] / "head_to_head.json",
        "operator_record": tree["root"] / "docs" / "evolution-system-delivery-ledger.md",
        "continuation_prompt": tree["root"] / "docs" / "national-tcp-evolution-continuation-prompt.md",
        "alignment_matrix": tree["root"] / "docs" / "national-tcp-evolution-alignment-matrix.md",
    }
    for label, path in denied.items():
        result = _invoke(handler, "Read", {"file_path": str(path)})
        assert _decision(result) == "deny", (label, _reason(result))


def test_bootstrap_scope_cannot_read_numeric_high_water(read_scope_tree):
    tree = read_scope_tree
    llm_query = tree["module"]
    hooks = llm_query._make_subagent_read_scope_guard(
        "MASTER PROPOSAL mechanism",
        [tree["target"]],
    )
    handler = hooks["PreToolUse"][0].hooks[0]

    allowed = _invoke(
        handler,
        "Read",
        {"file_path": str(tree["target"] / "policy.py")},
    )
    high_water = _invoke(
        handler,
        "Read",
        {
            "file_path": str(
                tree["root"] / "bots" / "national_v142" / "policy.py"
            )
        },
    )

    assert _decision(allowed) is None
    assert _decision(high_water) == "deny"
    assert "bot_not_in_role_scope" in _reason(high_water)


def test_master_evidence_guard_resolves_relative_paths_from_project_root(
    read_scope_tree,
    monkeypatch,
):
    tree = read_scope_tree
    llm_query = tree["module"]
    monkeypatch.setattr(llm_query, "_LLM_PROJECT_ROOT", tree["root"])

    allowed = llm_query._master_live_evidence_read_violation(
        "Read",
        {
            "file_path": (
                "web/core/results/v145/evidence_snapshot/"
                "selection_snapshot.json"
            )
        },
        allowed_evidence_snapshot_dir=tree["snapshot"],
    )
    foreign_live = llm_query._master_live_evidence_read_violation(
        "Read",
        {"file_path": "web/core/results/head_to_head.json"},
        allowed_evidence_snapshot_dir=tree["snapshot"],
    )

    assert allowed is None
    assert foreign_live == str(
        (tree["live_results"] / "head_to_head.json").resolve()
    )


def test_read_hook_rejects_parent_alias_and_symlink_escape(read_scope_tree):
    tree = read_scope_tree
    llm_query = tree["module"]
    link = tree["target"] / "linked_policy.py"
    link.symlink_to(tree["outside"])
    hooks = llm_query._make_subagent_read_scope_guard(
        "WORKER 1 (Architect)",
        [tree["target"]],
    )
    handler = hooks["PreToolUse"][0].hooks[0]

    parent_alias = tree["target"] / ".." / "national_v144" / "policy.py"
    alias_result = _invoke(
        handler,
        "Read",
        {"file_path": str(parent_alias)},
    )
    link_result = _invoke(
        handler,
        "Read",
        {"file_path": str(link)},
    )

    assert _decision(alias_result) == "deny"
    assert "parent_alias" in _reason(alias_result)
    assert _decision(link_result) == "deny"
    assert "symlink_path" in _reason(link_result)


@pytest.mark.parametrize(
    "command",
    (
        "cat {root}/bots/national_v142/policy.py",
        "rg decide {root}/bots/national_v146",
        "diff -u {target}/policy.py {root}/bots/national_v142/policy.py",
        "git log --max-count=1 HEAD",
        "git show HEAD:bots/national_v143/policy.py",
        "find {target} -type f",
        "python -c 'print(open(\"{root}/bots/national_v142/policy.py\").read())'",
        "python - <<'PY'\nprint(open('{target}/policy.py').read())\nPY",
        "bash -lc 'cat {root}/bots/national_v142/policy.py'",
        "sh -c 'cat {target}/policy.py'",
        "cat $TARGET/policy.py",
        "cat $(printf {target}/policy.py)",
        "cat {target}/*.py",
        "cat < {root}/bots/national_v142/policy.py",
        "PYTHONPATH={root}/bots/national_v142 python -m py_compile {target}/policy.py",
        "rg --ignore-file={root}/bots/national_v142/policy.py decide {target}",
        "grep --exclude-from {root}/bots/national_v142/policy.py decide {target}/policy.py",
        "wc --files0-from={root}/bots/national_v142/policy.py",
        "git -C {target} diff --no-index -- {target}/policy.py {target}/policy.py",
        "git diff --no-index --output={target}/probe.diff -- {target}/policy.py {target}/policy.py",
        "sed --in-place 's/decide/other/' {target}/policy.py",
        "sort --output={target}/sorted.txt",
        "rg decide",
    ),
)
def test_actual_bash_hook_blocks_indirect_and_out_of_scope_reads(
    read_scope_tree,
    command,
):
    tree = read_scope_tree
    llm_query = tree["module"]
    hooks = llm_query._make_subagent_read_scope_guard(
        "WORKER 1 (Architect)",
        [tree["target"]],
    )
    handler = hooks["PreToolUse"][1].hooks[0]
    rendered = command.format(
        root=tree["root"],
        target=tree["target"],
    )

    result = _invoke(handler, "Bash", {"command": rendered})

    assert _decision(result) == "deny", (rendered, _reason(result))


@pytest.mark.parametrize(
    "command",
    (
        "cat {target}/policy.py",
        "sed -n '1,20p' {target}/policy.py",
        "rg -n 'def decide' {target}",
        "diff -u {source}/policy.py {target}/policy.py",
        "git diff --no-index -- {source}/policy.py {target}/policy.py",
        "python -B -m py_compile {target}/policy.py",
        "(cd {target} && rg -n 'def decide' .)",
    ),
)
def test_actual_bash_hook_allows_bounded_explicit_reads(read_scope_tree, command):
    tree = read_scope_tree
    llm_query = tree["module"]
    hooks = llm_query._make_subagent_read_scope_guard(
        "LEAD CODE REVIEWER",
        [tree["source"], tree["target"]],
    )
    handler = hooks["PreToolUse"][1].hooks[0]
    rendered = command.format(source=tree["source"], target=tree["target"])

    result = _invoke(handler, "Bash", {"command": rendered})

    assert _decision(result) is None, (rendered, _reason(result))


def test_broad_project_grant_does_not_authorize_sensitive_subtrees(read_scope_tree):
    tree = read_scope_tree
    llm_query = tree["module"]
    scope = llm_query._normalize_allowed_read_scope([tree["root"]])

    bot_violation = llm_query._read_path_violation(
        tree["target"] / "policy.py",
        scope,
    )
    results_violation = llm_query._read_path_violation(
        tree["snapshot"] / "selection_snapshot.json",
        scope,
    )

    assert str(bot_violation).startswith("bot_not_in_role_scope:")
    assert str(results_violation).startswith("results_not_in_role_scope:")
