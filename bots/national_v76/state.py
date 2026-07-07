"""Tiny state helpers for the national-native bootstrap seed."""


def infer_remaining_hands_from_requests(requests):
    return max(0, 70 - len(requests or []))


def reconstruct_state(req):
    return dict(req or {})

