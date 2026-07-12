"""Glicko-2 + tanh 量级 + bb/100 CI 测试。

固化评分算法契约(贴合德扑实际:Glicko-2 主 + tanh 量级 + mbb/g CI)。
"""
from __future__ import annotations

from arena.backend.rating import (
    DEFAULT_RATING,
    DEFAULT_RD,
    DEFAULT_VOL,
    bb_per_100,
    ci_normal,
    score_binary,
    score_tanh,
    update,
)


def test_score_binary():
    assert score_binary(200) == 1.0
    assert score_binary(-200) == 0.0
    assert score_binary(0) == 0.5


def test_score_tanh_preserves_margin():
    # tanh 保留量级:大胜 > 小胜 > 微胜,且关于 0 对称
    s_big = score_tanh(5000)
    s_small = score_tanh(200)
    s_tiny = score_tanh(20)
    assert 0.5 < s_tiny < s_small < s_big < 1.0, (s_tiny, s_small, s_big)
    assert abs(score_tanh(0) - 0.5) < 1e-9
    assert abs(score_tanh(-1000) - (1 - score_tanh(1000))) < 1e-9  # 反对称


def test_update_win_raises_rating():
    nr, nrd, _ = update(DEFAULT_RATING, DEFAULT_RD, DEFAULT_VOL, 1500, 200, 1.0)
    assert nr > DEFAULT_RATING
    assert nrd < DEFAULT_RD  # RD 收敛(更确定)


def test_update_loss_lowers_rating():
    nr, _, _ = update(DEFAULT_RATING, DEFAULT_RD, DEFAULT_VOL, 1500, 200, 0.0)
    assert nr < DEFAULT_RATING


def test_update_draw_near_stable():
    nr, _, _ = update(DEFAULT_RATING, DEFAULT_RD, DEFAULT_VOL, 1500, 200, 0.5)
    assert abs(nr - DEFAULT_RATING) < 5


def test_beating_stronger_gains_more():
    nr_strong = update(DEFAULT_RATING, 200, DEFAULT_VOL, 1800, 100, 1.0)[0]
    nr_weak = update(DEFAULT_RATING, 200, DEFAULT_VOL, 1200, 100, 1.0)[0]
    assert nr_strong > nr_weak  # 击败强敌涨更多


def test_glickman2013_example_serial():
    # Glickman 2013 example(批量参考结果 1464/151)。
    # arena 一场一对手 -> 串行 update(与批量略有差异,故容忍区间)。
    r, rd, vol = 1500.0, 200.0, 0.06
    for opp_r, opp_rd, s in [(1400, 30, 1.0), (1550, 100, 0.0), (1700, 300, 0.0)]:
        r, rd, vol = update(r, rd, vol, opp_r, opp_rd, s, tau=0.5)
    assert r < 1500                  # 2 负 1 胜 -> 降
    assert 1440 < r < 1480, r
    assert rd < 200                  # RD 收敛


def test_tanh_distinguishes_margin_in_rating():
    # tanh 量级让 rating 更新区分大胜/小胜(binary W/L 不区分)
    nr_big = update(1500, 200, DEFAULT_VOL, 1500, 200, score_tanh(5000))[0]
    nr_small = update(1500, 200, DEFAULT_VOL, 1500, 200, score_tanh(100))[0]
    assert nr_small < nr_big


def test_bb_per_100():
    # 净 200 / BB 100 / 70 手 = 2/70*100 ≈ 2.857 bb/100
    assert abs(bb_per_100(200, 70) - (2.0 / 70 * 100)) < 1e-6
    assert bb_per_100(0, 70) == 0.0
    assert bb_per_100(200, 0) == 0.0
    assert bb_per_100(-200, 70) < 0


def test_ci_normal():
    mean, lo, hi = ci_normal([1.0, 2.0, 3.0])
    assert abs(mean - 2.0) < 1e-9
    assert lo < mean < hi
    m1, l1, h1 = ci_normal([5.0])  # n<2 退化
    assert m1 == l1 == h1 == 5.0
