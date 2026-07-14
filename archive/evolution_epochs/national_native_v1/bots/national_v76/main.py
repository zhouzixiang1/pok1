"""National-native bootstrap seed v76.

This seed is intentionally small and conservative. It exists to restart the
national-native epoch with a raw TCP compliant active bot after legacy newline
TCP bots were quarantined from the active pool.
"""


def sanitize_action(action, state, my_chips):
    try:
        return int(action)
    except (TypeError, ValueError):
        return 0

