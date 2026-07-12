"""Glicko-2 评分(贴合德扑实际:PokerBench tanh 量级 + ACPC mbb/g CI)。

Glicko-2(Glickman 2013)自实现,纯 stdlib。一场 = 1v1 单对手 rating period。

分数 S(本方 [0,1]):
  - score_binary(标准 Glicko): W/L/D -> 1.0/0.0/0.5(净筹码正/负/零)
  - score_tanh(PokerBench 增强,保留大胜/小胜量级): S = 0.5 + 0.5·tanh(net_bb/scale)
    比 W/L 信息量大,避免"W-L 掩盖 net-chips"陷阱。

参考:
  - Glickman "Example of the Glicko-2 system"(2013)
  - PokerBench(arXiv): Glicko-2 τ=0.5 + S=0.5+0.5·tanh(m)
  - ACPC: mbb/g(bb/100)+95% CI 真值锚
"""
from __future__ import annotations

import math
from collections.abc import Iterable

# Glicko-2 常量
GLICKO_SCALE = 173.7178  # rating <-> mu 尺度
DEFAULT_RATING = 1500.0
DEFAULT_RD = 350.0
DEFAULT_VOL = 0.06
DEFAULT_TAU = 0.5
BIG_BLIND = 100  # 国赛盲注 50/100, BB=100

# tanh 分数缩放:一场 net_bb 典型 ±20~50,tanh(net_bb/scale) 映射到约 [0.05, 0.95]
TANH_SCALE = 25.0


def score_binary(earnings: int) -> float:
    """标准 Glicko 二元分数:W/L/D -> 1.0/0.0/0.5(净筹码正/负/零)。"""
    if earnings > 0:
        return 1.0
    if earnings < 0:
        return 0.0
    return 0.5


def score_tanh(net_chips: int, big_blind: int = BIG_BLIND,
               scale: float = TANH_SCALE) -> float:
    """PokerBench tanh 分数:S = 0.5 + 0.5·tanh(net_bb/scale),保留大胜/小胜量级。

    net_bb = net_chips/big_blind。scale=25: net_bb=±50 -> S≈0.98/0.02,
    net_bb=±10 -> S≈0.69/0.31, net_bb=0 -> 0.5。比二元 W/L 信息量大。
    """
    net_bb = net_chips / big_blind
    return 0.5 + 0.5 * math.tanh(net_bb / scale)


def _g(phi: float) -> float:
    return 1.0 / math.sqrt(1.0 + 3.0 * phi * phi / (math.pi * math.pi))


def _e(mu: float, mu_j: float, phi_j: float) -> float:
    return 1.0 / (1.0 + math.exp(-_g(phi_j) * (mu - mu_j)))


def _new_volatility(vol: float, phi: float, v: float, delta: float,
                    tau: float) -> float:
    """Glickman volatility 迭代求根(Illinois 算法)。"""
    a = math.log(vol * vol)
    tau2 = tau * tau
    phi2 = phi * phi
    delta2 = delta * delta
    eps = 1e-6

    def f(x: float) -> float:
        ex = math.exp(x)
        d2 = phi2 + v + ex
        return (ex * (delta2 - phi2 - v)) / (2.0 * d2 * d2) - (x - a) / tau2

    A = a
    if delta2 > phi2 + v:
        B = math.log(delta2 - phi2 - v)
    else:
        k = 1
        while f(a - k * tau) < 0:
            k += 1
        B = a - k * tau
    fA, fB = f(A), f(B)
    while abs(B - A) > eps:
        C = A + (A - B) * fA / (fB - fA)
        fC = f(C)
        if fC * fB <= 0:
            A, fA = B, fB
        else:
            fA /= 2.0
        B, fB = C, fC
    return math.exp(A / 2.0)


def update(rating: float, rd: float, vol: float,
           opp_rating: float, opp_rd: float, score: float,
           tau: float = DEFAULT_TAU) -> tuple[float, float, float]:
    """单对手 Glicko-2 更新(一场一对手)。返回 (new_rating, new_rd, new_vol)。

    score: 本方分数 [0,1](binary W/L/D 或 tanh 量级)。
    """
    mu = (rating - 1500) / GLICKO_SCALE
    phi = rd / GLICKO_SCALE
    mu_j = (opp_rating - 1500) / GLICKO_SCALE
    phi_j = opp_rd / GLICKO_SCALE

    g_j = _g(phi_j)
    E_j = _e(mu, mu_j, phi_j)
    v = 1.0 / (g_j * g_j * E_j * (1 - E_j))
    delta = v * g_j * (score - E_j)

    new_vol = _new_volatility(vol, phi, v, delta, tau)
    phi_star = math.sqrt(phi * phi + new_vol * new_vol)
    new_phi = 1.0 / math.sqrt(1.0 / (phi_star * phi_star) + 1.0 / v)
    new_mu = mu + new_phi * new_phi * g_j * (score - E_j)

    return (new_mu * GLICKO_SCALE + 1500, new_phi * GLICKO_SCALE, new_vol)


def bb_per_100(net_chips: int, hands: int, big_blind: int = BIG_BLIND) -> float:
    """本方该场 bb/100 = (净筹码/大盲)/手数 × 100。ACPC mbb/g 的 bb/100 口径。

    例:净 200 / BB 100 / 70 手 = (2)/70×100 ≈ 2.86 bb/100。
    """
    if hands <= 0:
        return 0.0
    return (net_chips / big_blind) / hands * 100.0


def ci_normal(values: Iterable[float], z: float = 1.96) -> tuple[float, float, float]:
    """正态 CI(默认 95%):返回 (mean, ci_low, ci_high)。n<2 时 CI 退化为 mean。"""
    xs = list(values)
    n = len(xs)
    if n == 0:
        return (0.0, 0.0, 0.0)
    mean = sum(xs) / n
    if n < 2:
        return (mean, mean, mean)
    var = sum((x - mean) ** 2 for x in xs) / (n - 1)
    se = math.sqrt(var) / math.sqrt(n)
    return (mean, mean - z * se, mean + z * se)
