"""Archived local JSON poker engine.

Use :mod:`sever.engine` and the raw national TCP runtime for active evaluation.
"""

from .judge import judge
from .battle import battle, mirror_battle

__all__ = ["battle", "judge", "mirror_battle"]
