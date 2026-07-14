"""Training hyperparameters for HoldemRL.

Design follows DanLM's DanZeroV3Config pattern: cycle-based deterministic training.
Core hyperparameters (N, k, S) fully determine training behavior.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass
class HoldemRLConfig:
    """Hold'em RL training configuration.

    Core cycle parameters:
        N (replay_buffer_size): total replay buffer capacity
        k (replay_buffer_diversity): number of historical weight versions
        S (train_steps_per_cycle): gradient steps per sync cycle
        budget_per_cycle = N // k: samples collected per cycle
    """

    # Network architecture
    architecture: str = "mlp"  # "mlp" or "transformer"
    input_dim: int = 132       # OBS_DIM from holdem_env
    hidden_sizes: tuple[int, ...] = (512, 1024, 512, 1024, 512)
    dropout: float = 0.0

    # Transformer-specific (DanLM style)
    vocab_size: int = 80       # tokenizer vocab
    max_seq_len: int = 128     # max token sequence length
    d_model: int = 128         # transformer embedding dimension
    n_heads: int = 4           # attention heads
    n_layers: int = 4          # transformer layers
    d_ff: int = 512            # feedforward dimension

    # Optimization
    lr: float = 1e-4
    optimizer: str = "adam"
    weight_decay: float = 0.0
    grad_clip: float = 10.0

    # DMC core: cycle-based training
    replay_buffer_size: int = 100_000  # N
    replay_buffer_diversity: int = 2   # k
    train_steps_per_cycle: int = 16    # S (was 8, doubled for better buffer utilization)
    batch_size: int = 2048

    # Data collection: hands per actor per cycle
    # Total new data per cycle = actor_hands_per_cycle × num_actors
    # Each hand produces ~2-5 transitions, ~0.1s per hand on CPU
    # Recommended: 100-300 hands (10-30s per cycle with 4 actors)
    actor_hands_per_cycle: int = 100

    # Exploration
    exploration: str = "eps_greedy"  # eps_greedy or boltzmann
    eps_start: float = 0.3
    eps_end: float = 0.01
    eps_decay_steps: int = 5_000     # decay within ~500 cycles (8 steps/cycle)
    boltzmann_temp: float = 1.0

    # Reward shaping
    reward_scale: float = 1.0        # scale chip delta
    reward_clip: float = 100.0       # clip extreme rewards
    use_td_bootstrap: bool = False
    td_beta: float = 0.95

    # Dueling DQN
    dueling: bool = True

    # Double DQN
    double_dqn: bool = True
    target_update_freq: int = 500    # steps between target network sync
    target_update_tau: float = 0.005 # soft update coefficient

    # Actors
    num_actors: int = 4

    # Game settings
    initial_chips: int = 20000
    small_blind: int = 50
    big_blind: int = 100
    hands_per_eval_game: int = 50

    # Checkpointing
    ckpt_dir: str = "rl/checkpoints"
    ckpt_every_cycles: int = 10
    max_ckpts: int = 10

    # Evaluation
    eval_every_cycles: int = 10
    eval_num_games: int = 100
    eval_opponent: str = "random"  # random, bot1, bot5, etc.

    # Device
    device: str = "cuda"

    @property
    def budget_per_cycle(self) -> int:
        return self.replay_buffer_size // self.replay_buffer_diversity

    def to_dict(self) -> dict:
        d = asdict(self)
        d["hidden_sizes"] = list(d["hidden_sizes"])
        return d

    @classmethod
    def from_dict(cls, d: dict) -> HoldemRLConfig:
        d = dict(d)
        if "hidden_sizes" in d:
            d["hidden_sizes"] = tuple(d["hidden_sizes"])
        valid_keys = {f.name for f in cls.__dataclass_fields__.values()}
        d = {k: v for k, v in d.items() if k in valid_keys}
        return cls(**d)
