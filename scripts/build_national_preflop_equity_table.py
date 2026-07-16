#!/usr/bin/env python3
"""Reproduce the strict runtime's compact 169-class heads-up equity facts.

This is an offline, stdlib-only build utility.  It samples a uniformly random
opponent holding and five-card board from the 50 cards left after one canonical
hero holding.  Runtime bots never execute this script; they load only the
content-bound 169-float literal in the system ``precompute.py`` template.
"""

from __future__ import annotations

import argparse
import hashlib
import multiprocessing as mp
from pathlib import Path
import platform
import random
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from sever.engine.deck import Card  # noqa: E402
from sever.engine.evaluator import best_hand as official_best_hand  # noqa: E402


METHOD = "fixed_seed_uniform_opponent_board_mc_v1"
SAMPLES_PER_CLASS = 65_536
BASE_SEED = 0x4E4154494F4E414C
CLASS_SEED_MULTIPLIER = 0x9E3779B97F4A7C15
PYTHON_IMPLEMENTATION = "CPython"
PYTHON_VERSION = "3.14.4"
RANDOM_SOURCE_SHA256 = (
    "62dca8cdae7482513b99bb093ff038afd5131954e7eb78166d673a772cee871c"
)
EVALUATOR_SOURCE = "sever/engine/evaluator.py"
EVALUATOR_SHA256 = (
    "9992ee2608db9aef0320a586117f9ced8bdf33ad79581b9356686210cabd425f"
)
CARD_SOURCE = "sever/engine/deck.py"
CARD_SOURCE_SHA256 = (
    "8afb902bc936bca5659997e9b36a923d69304946f5659b35c054cd8c702851d5"
)
_MASK_64 = (1 << 64) - 1
_CARDS = tuple(Card(card % 4, card // 4) for card in range(52))
_BUILD_ENVIRONMENT_VALIDATED = False


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_build_environment() -> None:
    """Fail closed unless every table-producing dependency is exact."""

    global _BUILD_ENVIRONMENT_VALIDATED
    if _BUILD_ENVIRONMENT_VALIDATED:
        return
    observed = {
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
        "random_source_sha256": _sha256_file(Path(random.__file__ or "")),
        "evaluator_sha256": _sha256_file(ROOT / EVALUATOR_SOURCE),
        "card_source_sha256": _sha256_file(ROOT / CARD_SOURCE),
    }
    expected = {
        "python_implementation": PYTHON_IMPLEMENTATION,
        "python_version": PYTHON_VERSION,
        "random_source_sha256": RANDOM_SOURCE_SHA256,
        "evaluator_sha256": EVALUATOR_SHA256,
        "card_source_sha256": CARD_SOURCE_SHA256,
    }
    if observed != expected:
        raise RuntimeError(
            "preflop equity build environment mismatch: "
            f"expected={expected!r}:observed={observed!r}"
        )
    _BUILD_ENVIRONMENT_VALIDATED = True


def initialize_worker() -> None:
    validate_build_environment()


def _best_hand_rank(cards) -> tuple:
    return official_best_hand([_CARDS[int(card)] for card in cards])[0]


def representative_hole(class_index: int) -> tuple[int, int]:
    """Map row-major pair/suited/offsuit class identity to one isomorph."""

    row, column = divmod(int(class_index), 13)
    if not 0 <= row < 13 or not 0 <= column < 13:
        raise ValueError("class index must be in 0..168")
    if row == column:
        return row * 4, row * 4 + 1
    if row > column:  # lower triangle: suited
        return row * 4, column * 4
    return row * 4, column * 4 + 1  # upper triangle: offsuit


def class_seed(class_index: int) -> int:
    return BASE_SEED ^ ((int(class_index) * CLASS_SEED_MULTIPLIER) & _MASK_64)


def estimate_class(task: tuple[int, int]) -> tuple[int, float]:
    class_index, samples = map(int, task)
    if samples <= 0:
        raise ValueError("samples must be positive")
    initialize_worker()
    hero = representative_hole(class_index)
    deck = [card for card in range(52) if card not in hero]
    generator = random.Random(class_seed(class_index))
    points = 0.0
    for _ in range(samples):
        drawn = generator.sample(deck, 7)
        opponent, board = drawn[:2], drawn[2:]
        hero_rank = _best_hand_rank((*hero, *board))
        opponent_rank = _best_hand_rank((*opponent, *board))
        if hero_rank > opponent_rank:
            points += 1.0
        elif hero_rank == opponent_rank:
            points += 0.5
    return class_index, round(points / samples, 6)


def build_table(
    *,
    samples: int = SAMPLES_PER_CLASS,
    workers: int = 1,
    indices=range(169),
) -> dict[int, float]:
    tasks = [(int(index), int(samples)) for index in indices]
    if int(workers) <= 1:
        initialize_worker()
        rows = map(estimate_class, tasks)
    else:
        with mp.Pool(
            processes=int(workers),
            initializer=initialize_worker,
        ) as pool:
            rows = pool.map(estimate_class, tasks)
    return dict(sorted(rows))


def render_table(values: dict[int, float]) -> str:
    if set(values) != set(range(169)):
        raise ValueError("render requires all 169 classes")
    lines = ["PREFLOP_CLASS_EQUITY = ("]
    for offset in range(0, 169, 13):
        lines.append(
            "    "
            + ", ".join(
                f"{values[index]:.6f}"
                for index in range(offset, offset + 13)
            )
            + ","
        )
    lines.append(")")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=SAMPLES_PER_CLASS)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument(
        "--indices",
        default="",
        help="comma-separated 0..168 subset; empty builds/renders all classes",
    )
    args = parser.parse_args()
    indices = (
        [int(value) for value in args.indices.split(",") if value.strip()]
        if args.indices
        else list(range(169))
    )
    values = build_table(
        samples=args.samples,
        workers=args.workers,
        indices=indices,
    )
    if len(values) == 169:
        print(render_table(values))
    else:
        for index, value in values.items():
            print(f"{index}:{value:.6f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
