import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PROMPTS = ROOT / "web" / "core" / "prompts"


def test_orchestrator_prompt_uses_checkpoint_attempt_contract():
    prompt = (PROMPTS / "orchestrator.md").read_text(encoding="utf-8")

    assert "Do NOT keep a private `intra_gen_attempts` counter" in prompt
    assert "tool return fields are authoritative" in prompt
    assert "`generation_attempt`" in prompt
    assert "`precommit_attempt`" in prompt
    assert "Track `intra_gen_attempts`" not in prompt
    assert "Total intra_gen_attempts must not exceed" not in prompt


def test_orchestrator_prompt_keeps_critic_advisory_before_native_precommit():
    prompt = (PROMPTS / "orchestrator.md").read_text(encoding="utf-8")

    assert "Critic score is advisory" in prompt
    assert "always call `run_precommit_eval`" in prompt
    assert "native-TCP precommit" in prompt
    assert "Critic rejection is a hard strategy gate" not in prompt


def test_orchestrator_prompt_treats_master_error_as_blocking():
    prompt = (PROMPTS / "orchestrator.md").read_text(encoding="utf-8")
    normalized = " ".join(prompt.split())

    assert 'If the result contains `"error"` → Master FAILED' in prompt
    assert 'contains `"plan"` key and NO `"error"` key' in prompt
    assert "`worker_prompt` hard-size violations are BLOCKING" in normalized
    assert 'If the result contains `"plan"` key → Master SUCCEEDED' not in prompt
    assert "worker_prompt size warnings are ADVISORY" not in prompt


def test_orchestrator_prompt_crossover_commit_contract_is_consistent():
    prompt = (PROMPTS / "orchestrator.md").read_text(encoding="utf-8")

    assert "including a crossover generation" in prompt
    assert "supplies only the `prepared` baseline" in prompt
    assert "it never substitutes for Master or\n   Worker execution" in prompt
    assert "crossover performs\nno independent strategy mutation" in prompt
    assert "checkpoint is at `workers_done`" not in prompt


def test_llm_stages_documents_active_strict_control_contract():
    stages = (ROOT / "docs" / "llm-stages.md").read_text(encoding="utf-8")

    assert "## One active protocol" in stages
    assert "Candidate code may edit only `policy.py`" in stages
    assert "typed-intent validator" in stages
    assert "Master proposal ensemble" in stages
    assert "Precommit evaluation" in stages
    assert "Official certification" in stages


def test_llm_stages_documents_checkpoint_and_evaluation_authority():
    stages = (ROOT / "docs" / "llm-stages.md").read_text(encoding="utf-8")
    normalized = " ".join(stages.split())

    assert "immutable workflow run ID and monotonic CAS revision" in normalized
    assert "replays frozen inputs" in normalized
    assert "Critic" in normalized and "score is not an acceptance threshold" in normalized
    assert "five 70-hand self-play rounds and three 70-hand rounds" in normalized
    assert "Official chip results have zero strength weight" in normalized


def test_main_orchestrator_has_mcp_tools_but_no_builtin_tools_on_all_dispatches():
    source = (ROOT / "web" / "core" / "orchestrator.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    options_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "ClaudeAgentOptions"
    ]
    # Initial dispatch and the 529 retry must have identical tool authority.
    assert len(options_calls) == 2
    for call in options_calls:
        keywords = {item.arg: item.value for item in call.keywords if item.arg}
        assert isinstance(keywords.get("tools"), ast.List)
        assert keywords["tools"].elts == []
        servers = keywords.get("mcp_servers")
        assert isinstance(servers, ast.Dict)
        assert [key.value for key in servers.keys] == ["evolution"]
