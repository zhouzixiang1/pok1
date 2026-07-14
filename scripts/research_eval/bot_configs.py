"""Bot configurations for research evaluation."""

from pathlib import Path

REPO = Path("/home/zzx/project/pok")
WT_A = REPO / ".codex_worktrees/rebel-decisionholdem"
WT_B = REPO / ".codex_worktrees/cfr-neural-search"

BOTS = {
    "A1_rebel": {
        "entry": str(WT_A / "bots/research_native_lab/rebel_decisionholdem/rebel_like/native_entry.py"),
        "args": ["--deploy", "/tmp/m5b_run_v2/deploy.npz"],
        "cwd": str(WT_A),
        "description": "ReBeL-like: PBS + value/policy network",
    },
    "A2_decisionholdem": {
        "entry": str(WT_A / "bots/research_native_lab/rebel_decisionholdem/decisionholdem_like/native_entry.py"),
        "args": ["--blueprint", str(WT_A / "bots/research_native_lab/rebel_decisionholdem/artifacts/hunl_m4_smoke_blueprint.json")],
        "cwd": str(WT_A),
        "description": "DecisionHoldem-like: Linear CFR blueprint + resolver",
    },
    "B_cfr_neural": {
        "entry": str(WT_B / "bots/research_native_lab/cfr_neural_search/native_runtime/neural_native_entry.py"),
        "args": ["--artifact", str(WT_B / "bots/research_native_lab/cfr_neural_search/artifacts/m4/blueprint.rbbp"),
                 "--cfv-model", "/tmp/cfv_model_b_v1.pt"],
        "cwd": str(WT_B),
        "description": "CFR blueprint + neural CFV leaf values",
    },
}

# All pairwise H2H matchups
H2H_PAIRS = [
    ("A1_rebel", "A2_decisionholdem"),
    ("A1_rebel", "B_cfr_neural"),
    ("A2_decisionholdem", "B_cfr_neural"),
]
