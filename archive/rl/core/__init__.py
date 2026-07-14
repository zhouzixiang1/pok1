"""HoldemRL — Heads-up No-Limit Texas Hold'em RL training framework.

Architecture inspired by DanLM ("Tokenization Is All You Need to Master Complex Card Games").

Directory structure:
    rl/
    ├── core/
    │   ├── __init__.py         # Package init
    │   ├── holdem_env.py       # Gymnasium environment wrapping engine/judge.py
    │   ├── tokenizer.py        # Game history tokenization (~50 vocab)
    │   ├── encoder.py          # State/action encoders (v0 hand-crafted, v1 token-based)
    │   └── config.py           # Training hyperparameters (DanZeroV3Config style)
    ├── models/
    │   ├── __init__.py
    │   ├── q_network.py        # MLP Q-network (DanZero style)
    │   ├── transformer.py      # TinyLM Transformer Q-network (DanLM style)
    │   └── network_utils.py    # Weight init, etc.
    ├── training/
    │   ├── __init__.py
    │   ├── replay_buffer.py    # Prioritized replay buffer
    │   ├── actor.py            # Self-play actor (collects samples)
    │   ├── learner.py          # Learner (trains from replay buffer)
    │   └── trainer.py          # DMC training loop coordinator
    ├── eval/
    │   ├── __init__.py
    │   └── evaluator.py        # Evaluation vs baseline bots
    ├── scripts/
    │   ├── train.py            # Training entry point
    │   └── evaluate.py         # Evaluation entry point
    └── requirements.txt
"""
