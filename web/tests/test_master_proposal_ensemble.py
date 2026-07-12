import json
import re

import pytest


class _UI:
    def clear_io(self):
        pass

    def log_history(self, *_args, **_kwargs):
        pass


def _proposal(direction: str) -> str:
    payload = {
        "targeted_failure": f"{direction} identifies one repeated reachable decision failure.",
        "structural_change": f"{direction} replaces the parent decision mechanism with one bounded path.",
        "counterfactual": f"{direction} changes one input while holding cards, seed, and legality fixed.",
        "measurement": f"{direction} requires a positive/control decision test and paired native result.",
        "why_not_threshold_tuning": f"{direction} changes state flow and a reachable consumer, not one number.",
        "target_files": ["strategy.py"],
        "risks": "The mechanism may overfit sparse evidence and must remain bounded.",
    }
    return "```json\n" + json.dumps(payload) + "\n```"


@pytest.mark.asyncio
async def test_proposal_ensemble_samples_three_and_blind_reviews_two(monkeypatch, tmp_path):
    import agent_master

    calls = []

    async def fake_query(
        prompt, _ctx, _ui, role_name, _log_file, tools=None, **kwargs
    ):
        calls.append((role_name, tools, kwargs))
        if role_name.startswith("MASTER PROPOSAL CRITIC"):
            ids = list(dict.fromkeys(re.findall(r'"proposal_id":\s*"([0-9a-f]{16})"', prompt)))
            ranking = list(reversed(ids)) if role_name.endswith("scope") else ids
            return json.dumps({"ranking": ranking, "reject": [], "reason": "bounded review"}), 0.0, {}
        direction = role_name.rsplit(" ", 1)[-1]
        return _proposal(direction), 0.0, {}

    monkeypatch.setattr(agent_master, "run_claude_query", fake_query)
    packet_text = await agent_master._run_master_proposal_ensemble(
        "frozen planning context",
        source_v=140,
        next_v=149,
        ui=_UI(),
        log_dir=tmp_path,
        allowed_evidence_snapshot_dir=str(tmp_path / "snapshot"),
    )
    packet = json.loads(packet_text)

    assert packet["proposal_count"] == 3
    assert packet["valid_critic_count"] == 2
    assert len(packet["ordered_proposals"]) == 3
    assert len(calls) == 5
    scout_calls = calls[:3]
    assert all(call[1] == ["Read"] for call in scout_calls)
    assert all("allowed_evidence_snapshot_dir" in call[2] for call in scout_calls)
    assert all(call[1] == [] for call in calls[3:])
    assert packet["authority"].startswith("advisory_only")


def test_master_proposal_validation_rejects_source_override():
    import agent_master

    payload = json.loads(_proposal("mechanism").split("```json\n", 1)[1].rsplit("\n```", 1)[0])
    payload["branch_from"] = "v120"
    assert agent_master._validated_master_proposal(
        json.dumps(payload), "mechanism"
    ) is None
