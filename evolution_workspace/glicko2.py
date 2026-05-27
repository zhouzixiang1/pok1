"""
Glicko-2 Rating System
Based on Professor Mark Glickman's algorithm:
http://www.glicko.net/glicko/glicko2.pdf

Each player maintains three values:
  r     - rating (default 1500)
  rd    - rating deviation, measures uncertainty (default 350)
  sigma - volatility, measures expected fluctuation (default 0.06)

95% confidence interval: r +/- 2*rd
"""

import math

SCALE = 173.7
TAU = 0.5
EPSILON = 0.000001

DEFAULT_R = 1500.0
DEFAULT_RD = 350.0
DEFAULT_SIGMA = 0.06


class Glicko2Player:
    __slots__ = ('r', 'rd', 'sigma')

    def __init__(self, r=DEFAULT_R, rd=DEFAULT_RD, sigma=DEFAULT_SIGMA):
        self.r = r
        self.rd = rd
        self.sigma = sigma

    def to_dict(self):
        return {"r": self.r, "rd": self.rd, "sigma": self.sigma}

    @staticmethod
    def from_dict(d):
        return Glicko2Player(
            r=d.get("r", DEFAULT_R),
            rd=d.get("rd", DEFAULT_RD),
            sigma=d.get("sigma", DEFAULT_SIGMA),
        )

    def conservative_rating(self):
        """95% confidence lower bound, useful for ranking with uncertainty."""
        return self.r - 2 * self.rd

    def __repr__(self):
        return f"Glicko2Player(r={self.r:.1f}, rd={self.rd:.1f}, sigma={self.sigma:.4f})"


def _g(phi):
    return 1.0 / math.sqrt(1.0 + 3.0 * phi * phi / (math.pi * math.pi))


def _E(mu, mu_j, phi_j):
    return 1.0 / (1.0 + math.exp(-_g(phi_j) * (mu - mu_j)))


def update_rating_period(player, results):
    """
    Update a player's rating after one rating period.

    player: Glicko2Player with pre-period (r, rd, sigma)
    results: list of (opponent_player, score) tuples
             score = 1.0 (win), 0.0 (loss), 0.5 (draw)

    Returns: new Glicko2Player with updated values.

    If results is empty, only RD increases (player didn't play).
    """
    # Step 2: Convert to Glicko-2 scale
    mu = (player.r - DEFAULT_R) / SCALE
    phi = player.rd / SCALE

    # Step 3: Compute g(phi_j) and E(mu, mu_j, phi_j) for each opponent
    if not results:
        # Step 6: Player didn't play, only increase RD
        phi_star = math.sqrt(phi * phi + player.sigma * player.sigma)
        player_new = Glicko2Player(player.r, phi_star * SCALE, player.sigma)
        return player_new

    opponents_mu = []
    opponents_phi = []
    scores = []
    for opp, score in results:
        opponents_mu.append((opp.r - DEFAULT_R) / SCALE)
        opponents_phi.append(opp.rd / SCALE)
        scores.append(score)

    # Step 4: Compute estimated variance v and improvement sum delta
    v_inv = 0.0
    delta_sum = 0.0
    for j in range(len(results)):
        g_j = _g(opponents_phi[j])
        e_j = _E(mu, opponents_mu[j], opponents_phi[j])
        v_inv += g_j * g_j * e_j * (1.0 - e_j)
        delta_sum += g_j * (scores[j] - e_j)

    if v_inv <= 0.0:
        # Degenerate case: all opponents at extreme rating difference
        phi_star = math.sqrt(phi * phi + player.sigma * player.sigma)
        return Glicko2Player(player.r, phi_star * SCALE, player.sigma)

    v = 1.0 / v_inv
    delta = delta_sum * v

    # Step 5: Determine new sigma
    a = math.log(player.sigma * player.sigma)
    delta_sq = delta * delta
    phi_sq = phi * phi

    def f(x):
        ex = math.exp(x)
        num = ex * (delta_sq - phi_sq - v - ex)
        denom = 2.0 * (phi_sq + v + ex) ** 2
        return num / denom - (x - a) / (TAU * TAU)

    # Illinois algorithm for finding sigma
    A = a
    B = 0.0
    if delta_sq > phi_sq + v:
        B = math.log(delta_sq - phi_sq - v)
    else:
        k = 1
        while f(a - k * TAU) < 0:
            k += 1
        B = a - k * TAU

    fA = f(A)
    fB = f(B)

    for _ in range(1000):
        if abs(B - A) <= EPSILON:
            break
        denom = fB - fA
        if denom == 0.0:
            break
        C = A + (A - B) * fA / denom
        fC = f(C)
        if fC * fB <= 0:
            A = B
            fA = fB
        else:
            fA = fA / 2.0
        B = C
        fB = fC

    sigma_star = math.exp(A / 2.0)

    # Step 6: Update phi to new pre-rating period value
    phi_star = math.sqrt(phi_sq + sigma_star * sigma_star)

    # Step 7: Update rating and RD
    phi_new = 1.0 / math.sqrt(1.0 / (phi_star * phi_star) + 1.0 / v)
    mu_new = mu + phi_new * phi_new * delta_sum

    # Step 8: Convert back to original scale
    r_new = mu_new * SCALE + DEFAULT_R
    rd_new = phi_new * SCALE

    return Glicko2Player(r_new, rd_new, sigma_star)


def decay_rd(player, elapsed_periods=1):
    """
    Increase RD for a player who hasn't played recently.
    Called when a player is inactive for one or more rating periods.
    """
    mu = (player.r - DEFAULT_R) / SCALE
    phi = player.rd / SCALE
    phi_star = math.sqrt(phi * phi + player.sigma * player.sigma * elapsed_periods)
    return Glicko2Player(player.r, phi_star * SCALE, player.sigma)
