"""National-native bootstrap seed v77."""


def sanitize_action(action, state, my_chips):
    try:
        return int(action)
    except (TypeError, ValueError):
        return -1

