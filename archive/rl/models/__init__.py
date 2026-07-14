"""Q-Network architectures for Hold'em RL.

Two variants:
1. MLP Q-Network (DanZero style): flat observation → MLP → Q(s,a)
2. Transformer Q-Network (DanLM style): token sequence → TinyLM → context → Q-Head → Q(s,a)
"""
