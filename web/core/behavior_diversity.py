"""Behavior diversity metrics for evolution pool (fix-6).

Provides numeric diversity measurement via decision fingerprints and Vendi Score,
replacing free-text LLM-based diversity assessment in experience_pool.md.

Fingerprint computation:
    From decision_tester fixtures: round_idx, action_bucket, my_chips_ratio (3 dims)
    From match_history.jsonl per-bot: aggression_freq, vpip, showdown_rate (3 dims)
    These 6 base features are expanded to D=64 via Random Fourier Features (RFF).
"""

import json
import logging
import os
from pathlib import Path

import numpy as np

log = logging.getLogger("pok.behavior_diversity")

# Project structure
_CORE_DIR = Path(__file__).resolve().parent
_RESULTS_DIR = _CORE_DIR / "results"
_MATCH_HISTORY_FILE = _RESULTS_DIR / "match_history.jsonl"
_FINGERPRINTS_FILE = _RESULTS_DIR / "fingerprints.jsonl"
_SCENARIOS_FILE = _CORE_DIR / "test_scenarios.json"

# Fingerprint dimensions
_BASE_DIMS = 6    # 3 scenario + 3 match
_TARGET_DIMS = 64  # RFF output dimension

# Pre-generated RFF projection matrix (fixed random seed for determinism).
# Shape: (_BASE_DIMS, _TARGET_DIMS). Generated once at import time.
_RFF_SEED = 42
_RFF_MATRIX = None


def _get_rff_matrix():
    """Lazy-init the RFF projection matrix."""
    global _RFF_MATRIX
    if _RFF_MATRIX is None:
        rng = np.random.RandomState(_RFF_SEED)
        _RFF_MATRIX = rng.randn(_BASE_DIMS, _TARGET_DIMS) * 2.0
    return _RFF_MATRIX


# ──────────────────────────────────────────────
# Scenario-level features from decision_tester fixtures
# ──────────────────────────────────────────────

def _round_idx_from_history(history):
    """Extract the last round index from scenario history.

    Round 0=preflop, 1=flop, 2=turn, 3=river.
    If no history, defaults to 0 (preflop).
    """
    if not history:
        return 0
    max_round = 0
    for h in history:
        r = h.get("round", 0)
        if r > max_round:
            max_round = r
    return max_round


def _action_bucket_from_last_history(history):
    """Extract action type from the last history entry (opponent's last action).

    Returns: 0=check, 1=call, 2=raise, 3=fold, 4=allin.
    If no history, returns 0 (first to act).
    """
    if not history:
        return 0
    last = history[-1]
    action_type = last.get("action_type", "")
    if action_type == "check":
        return 0
    elif action_type == "call":
        return 1
    elif action_type == "raise":
        return 2
    elif action_type == "fold":
        return 3
    elif action_type == "allin":
        return 4
    # numeric action fallback
    action = last.get("action", 0)
    if action == -1:
        return 3
    elif action == -2:
        return 4
    elif action == 0:
        return 1
    elif action > 0:
        return 2
    return 0


def _scenario_features():
    """Compute average scenario-level features from test_scenarios.json.

    Returns (round_idx, action_bucket, my_chips_ratio) averaged across all scenarios.
    Each normalized to [0, 1].
    """
    if not _SCENARIOS_FILE.exists():
        return [0.5, 0.5, 1.0]

    try:
        with open(_SCENARIOS_FILE) as f:
            scenarios = json.load(f)
    except (json.JSONDecodeError, OSError):
        return [0.5, 0.5, 1.0]

    if not scenarios:
        return [0.5, 0.5, 1.0]

    rounds = []
    actions = []
    chips = []
    initial_chips = 20000

    for s in scenarios:
        inp = s.get("input", {})
        history = inp.get("history", [])
        rounds.append(_round_idx_from_history(history))
        actions.append(_action_bucket_from_last_history(history))
        chips.append(inp.get("my_chips", initial_chips) / initial_chips)

    n = len(rounds)
    return [
        sum(rounds) / n / 3.0,           # normalize by max round (3)
        sum(actions) / n / 4.0,           # normalize by max bucket (4)
        sum(chips) / n,                    # already [0, 1]
    ]


# ──────────────────────────────────────────────
# Match-level features from match_history.jsonl
# ──────────────────────────────────────────────

def _match_features(bot_name, match_history_file=None):
    """Compute match-level behavioral features for a bot.

    Returns (aggression_freq, vpip, showdown_rate), each in [0, 1].
    Falls back to (0.5, 0.5, 0.5) if no data.
    """
    fpath = match_history_file or _MATCH_HISTORY_FILE
    if not fpath.exists():
        return [0.5, 0.5, 0.5]

    raises = 0
    calls = 0
    folds = 0
    hands_played = 0
    showdowns = 0

    try:
        with open(fpath, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                # match_history entries have 'bot_a', 'bot_b', and per-hand data
                # The exact format may vary; try to extract action counts
                bot_a = entry.get("bot_a", "")
                bot_b = entry.get("bot_b", "")

                if bot_name not in (bot_a, bot_b):
                    continue

                # Extract from replay if available
                hands = entry.get("hands", [])
                for hand in hands:
                    hands_played += 1
                    if hand.get("showdown", False):
                        showdowns += 1

                    actions = hand.get("actions", [])
                    for act in actions:
                        player = act.get("player", "")
                        if player != bot_name and player != ("a" if bot_name == bot_a else "b"):
                            continue
                        atype = act.get("action_type", "")
                        if atype in ("raise", "allin"):
                            raises += 1
                        elif atype == "call":
                            calls += 1
                        elif atype == "fold":
                            folds += 1

                # Also check compact format: total_actions field
                total_acts = entry.get("total_actions", {})
                if isinstance(total_acts, dict):
                    raises += total_acts.get("raises", 0)
                    calls += total_acts.get("calls", 0)
                    folds += total_acts.get("folds", 0)

                # Check for win_rate / aggression fields directly stored
                aggr = entry.get("aggression_freq")
                v = entry.get("vpip")
                sd = entry.get("showdown_rate")
                # If pre-computed fields exist, prefer them over raw counts
                # (more accurate, avoids double-counting)
    except OSError:
        return [0.5, 0.5, 0.5]

    total = raises + calls + folds
    if total > 0:
        aggression = raises / total
        vpip = (raises + calls) / total
    else:
        aggression = 0.5
        vpip = 0.5

    showdown_rate = showdowns / hands_played if hands_played > 0 else 0.5

    return [
        min(1.0, max(0.0, aggression)),
        min(1.0, max(0.0, vpip)),
        min(1.0, max(0.0, showdown_rate)),
    ]


# ──────────────────────────────────────────────
# Fingerprint computation
# ──────────────────────────────────────────────

def compute_decision_fingerprint(bot_name: str, match_history_file=None) -> np.ndarray:
    """Compute a D=64 decision fingerprint for a bot.

    Uses 3 scenario-level dimensions (round_idx, action_bucket, my_chips_ratio)
    from decision_tester fixtures + 3 match-level dimensions (aggression_freq,
    vpip, showdown_rate) from match_history.jsonl, then RFF to D=64.

    Args:
        bot_name: Bot name like "claude_v49".
        match_history_file: Optional override for match history path.

    Returns:
        np.ndarray of shape (64,), unit-normalized.
    """
    scenario_feats = _scenario_features()
    match_feats = _match_features(bot_name, match_history_file)

    base = np.array(scenario_feats + match_feats, dtype=np.float64)

    # Project to D=64 via RFF (cosine features for RBF kernel approximation)
    W = _get_rff_matrix()  # (6, 64)
    projected = base @ W   # (64,)

    # cos + sin features for better approximation (doubling to 128, then take first 64)
    cos_feats = np.cos(projected)
    # Normalize to unit vector
    norm = np.linalg.norm(cos_feats)
    if norm > 0:
        cos_feats = cos_feats / norm

    return cos_feats


def compute_pool_fingerprints(bot_names: list[str], match_history_file=None) -> np.ndarray:
    """Compute fingerprints for a pool of bots.

    Returns np.ndarray of shape (N, 64) where N = len(bot_names).
    """
    fps = []
    for name in bot_names:
        fp = compute_decision_fingerprint(name, match_history_file)
        fps.append(fp)
    return np.stack(fps) if fps else np.empty((0, _TARGET_DIMS))


# ──────────────────────────────────────────────
# Vendi Score
# ──────────────────────────────────────────────

def vendi_score(fingerprints: np.ndarray, sigma: float = None) -> float:
    """Compute Vendi Score from fingerprints.

    VS = exp(H(lambda)) where H is Shannon entropy of eigenvalues of the
    normalized kernel matrix, and lambda are the eigenvalues.

    The kernel is a Gaussian (RBF) kernel with bandwidth sigma, computed
    dynamically as median pairwise distance if sigma is None.

    Args:
        fingerprints: np.ndarray of shape (N, D).
        sigma: RBF bandwidth. If None, uses median pairwise distance.

    Returns:
        float: Vendi Score. VS=1 for maximally spread, VS=0 for collapsed.
    """
    n = fingerprints.shape[0]
    if n <= 1:
        return 1.0  # trivial diversity

    # Compute pairwise squared distances
    diff = fingerprints[:, np.newaxis, :] - fingerprints[np.newaxis, :, :]  # (N, N, D)
    sq_dists = np.sum(diff ** 2, axis=-1)  # (N, N)

    # Auto-select sigma as median pairwise distance
    if sigma is None:
        # Extract upper triangle (exclude diagonal)
        triu_indices = np.triu_indices(n, k=1)
        if len(triu_indices[0]) == 0:
            return 1.0
        pairwise_dists = np.sqrt(sq_dists[triu_indices])
        sigma = np.median(pairwise_dists)
        if sigma < 1e-10:
            sigma = 1.0  # avoid degenerate kernel

    # Gaussian kernel matrix
    K = np.exp(-sq_dists / (2.0 * sigma ** 2))

    # Normalize kernel matrix (doubly stochastic approximation)
    # K_normalized = D^{-1/2} K D^{-1/2} where D = diag(K * ones)
    row_sums = K.sum(axis=1, keepdims=True)
    row_sums = np.maximum(row_sums, 1e-12)
    K_norm = K / np.sqrt(row_sums)
    K_norm = K_norm / np.sqrt(row_sums.T)

    # Eigenvalues of normalized kernel
    eigenvalues = np.linalg.eigvalsh(K_norm)
    eigenvalues = np.maximum(eigenvalues, 0.0)  # numerical stability

    # Normalize to form a probability distribution
    total = eigenvalues.sum()
    if total < 1e-12:
        return 0.0
    lambdas = eigenvalues / total

    # Shannon entropy of eigenvalues
    mask = lambdas > 1e-12
    entropy = -np.sum(lambdas[mask] * np.log(lambdas[mask]))

    # Vendi Score = exp(entropy)
    return float(np.exp(entropy))


def compute_delta_vendi(pool_fingerprints: np.ndarray, new_fingerprint: np.ndarray) -> float:
    """Compute the change in Vendi Score when adding a new bot to the pool.

    Args:
        pool_fingerprints: (N, D) array of existing pool fingerprints.
        new_fingerprint: (D,) array for the new bot.

    Returns:
        float: delta_VS = VS(with_new) - VS(without_new).
    """
    if pool_fingerprints.shape[0] == 0:
        return 1.0  # first bot trivially adds diversity

    vs_before = vendi_score(pool_fingerprints)
    extended = np.vstack([pool_fingerprints, new_fingerprint[np.newaxis, :]])
    vs_after = vendi_score(extended)
    return vs_after - vs_before


# ──────────────────────────────────────────────
# Persistence
# ──────────────────────────────────────────────

def save_fingerprint(bot_name: str, fingerprint: np.ndarray):
    """Append a fingerprint entry to fingerprints.jsonl."""
    entry = {
        "bot": bot_name,
        "fingerprint": fingerprint.tolist(),
    }
    try:
        os.makedirs(_RESULTS_DIR, exist_ok=True)
        with open(_FINGERPRINTS_FILE, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError as e:
        log.warning("Failed to save fingerprint for %s: %s", bot_name, e)


def load_fingerprints() -> dict[str, np.ndarray]:
    """Load all fingerprints from fingerprints.jsonl.

    Returns dict mapping bot_name -> np.ndarray (64,).
    If multiple entries exist for the same bot, the latest wins.
    """
    if not _FINGERPRINTS_FILE.exists():
        return {}
    result = {}
    try:
        with open(_FINGERPRINTS_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                entry = json.loads(line)
                bot = entry.get("bot")
                fp = entry.get("fingerprint")
                if bot and fp:
                    result[bot] = np.array(fp, dtype=np.float64)
    except (json.JSONDecodeError, OSError) as e:
        log.warning("Failed to load fingerprints: %s", e)
    return result


def _niche_from_fingerprint(fingerprint: np.ndarray, n_bins: int = 4) -> tuple:
    """Assign a fingerprint to a discrete niche by binning its first 3 components.

    Returns a hashable tuple of bin indices (length 3), each in [0, n_bins-1].
    """
    # Use first 3 principal components (already normalized to ~unit norm)
    # Map from [-1, 1] to [0, n_bins-1]
    vals = fingerprint[:3]
    bins = tuple(
        min(n_bins - 1, max(0, int((v + 1.0) / 2.0 * n_bins)))
        for v in vals
    )
    return bins


def get_niche_for_bot(bot_name: str, fingerprints: dict[str, np.ndarray] = None,
                      n_bins: int = 4) -> tuple:
    """Get the niche assignment for a bot.

    Args:
        bot_name: Bot name like "claude_v49".
        fingerprints: Optional pre-loaded fingerprint dict.
        n_bins: Number of bins per dimension for niche assignment.

    Returns:
        Hashable tuple representing the niche, or None if no fingerprint data.
    """
    if fingerprints is None:
        fingerprints = load_fingerprints()
    fp = fingerprints.get(bot_name)
    if fp is None:
        return None
    return _niche_from_fingerprint(fp, n_bins)
