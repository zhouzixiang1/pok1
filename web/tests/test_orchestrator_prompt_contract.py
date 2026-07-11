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

    assert 'If the result contains `"error"` → Master FAILED' in prompt
    assert 'contains `"plan"` key and NO `"error"` key' in prompt
    assert "worker_prompt` hard-size\nviolations are BLOCKING" in prompt
    assert 'If the result contains `"plan"` key → Master SUCCEEDED' not in prompt
    assert "worker_prompt size warnings are ADVISORY" not in prompt


def test_orchestrator_prompt_crossover_commit_contract_is_consistent():
    prompt = (PROMPTS / "orchestrator.md").read_text(encoding="utf-8")

    assert "Crossover generation: `run_crossover` succeeded" in prompt
    assert "checkpoint at\n   `workers_done`" in prompt
    assert "do NOT call `run_direction_audit`, `run_master`, or\n   `execute_workers`" in prompt
    assert "1. `run_direction_audit` was called" not in prompt


def test_llm_stages_documents_current_mcp_tool_contract():
    stages = (ROOT / "docs" / "llm-stages.md").read_text(encoding="utf-8")
    tools_source = (ROOT / "web" / "core" / "tools.py").read_text(encoding="utf-8")

    assert "17 个工具" in stages
    assert "15 个工具" not in stages
    assert "(17 tools)" in tools_source
    assert "~15 tools" not in tools_source
    for tool_name in ("run_literature_probe", "abandon_generation"):
        assert tool_name in stages
        assert tool_name in tools_source


def test_llm_stages_documents_checkpoint_directed_retries():
    stages = (ROOT / "docs" / "llm-stages.md").read_text(encoding="utf-8")

    assert "代内重试循环（Checkpoint/工具指令驱动）" in stages
    assert "不要让 LLM 自己维护私有计数器" in stages
    assert "Critic 低分:" in stages
    assert "作为硬门拒绝" in stages
    assert "不允许 unchanged code 进入 run_precommit_eval" in stages
    assert "intra_gen_attempts = 0" not in stages
