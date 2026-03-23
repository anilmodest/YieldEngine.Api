import math
from scipy.stats import norm


RISK_FREE_RATE = 0.065  # 6.5% Indian risk-free rate


def d1(S, K, T, r, sigma):
    """Calculate d1 in Black-Scholes formula."""
    if T <= 0 or sigma <= 0:
        return 0.0
    return (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))


def d2(S, K, T, r, sigma):
    """Calculate d2 in Black-Scholes formula."""
    return d1(S, K, T, r, sigma) - sigma * math.sqrt(T)


def call_price(S, K, T, r=RISK_FREE_RATE, sigma=0.2):
    """European call option price."""
    if T <= 0:
        return max(S - K, 0)
    _d1 = d1(S, K, T, r, sigma)
    _d2 = d2(S, K, T, r, sigma)
    return S * norm.cdf(_d1) - K * math.exp(-r * T) * norm.cdf(_d2)


def put_price(S, K, T, r=RISK_FREE_RATE, sigma=0.2):
    """European put option price."""
    if T <= 0:
        return max(K - S, 0)
    _d1 = d1(S, K, T, r, sigma)
    _d2 = d2(S, K, T, r, sigma)
    return K * math.exp(-r * T) * norm.cdf(-_d2) - S * norm.cdf(-_d1)


def option_price(S, K, T, r=RISK_FREE_RATE, sigma=0.2, option_type="CE"):
    """Price a call or put option."""
    if option_type == "CE":
        return call_price(S, K, T, r, sigma)
    return put_price(S, K, T, r, sigma)


# --- Greeks ---

def delta(S, K, T, r=RISK_FREE_RATE, sigma=0.2, option_type="CE"):
    """Option delta."""
    if T <= 0 or sigma <= 0:
        if option_type == "CE":
            return 1.0 if S > K else 0.0
        return -1.0 if S < K else 0.0
    _d1 = d1(S, K, T, r, sigma)
    if option_type == "CE":
        return norm.cdf(_d1)
    return norm.cdf(_d1) - 1


def gamma(S, K, T, r=RISK_FREE_RATE, sigma=0.2):
    """Option gamma (same for calls and puts)."""
    if T <= 0 or sigma <= 0:
        return 0.0
    _d1 = d1(S, K, T, r, sigma)
    return norm.pdf(_d1) / (S * sigma * math.sqrt(T))


def theta(S, K, T, r=RISK_FREE_RATE, sigma=0.2, option_type="CE"):
    """Option theta (per day)."""
    if T <= 0 or sigma <= 0:
        return 0.0
    _d1 = d1(S, K, T, r, sigma)
    _d2 = d2(S, K, T, r, sigma)
    common = -(S * norm.pdf(_d1) * sigma) / (2 * math.sqrt(T))
    if option_type == "CE":
        return (common - r * K * math.exp(-r * T) * norm.cdf(_d2)) / 365
    return (common + r * K * math.exp(-r * T) * norm.cdf(-_d2)) / 365


def vega(S, K, T, r=RISK_FREE_RATE, sigma=0.2):
    """Option vega (per 1% change in IV)."""
    if T <= 0 or sigma <= 0:
        return 0.0
    _d1 = d1(S, K, T, r, sigma)
    return S * norm.pdf(_d1) * math.sqrt(T) / 100


# --- Implied Volatility Solver (Newton-Raphson) ---

def implied_volatility(market_price, S, K, T, r=RISK_FREE_RATE, option_type="CE",
                       max_iterations=100, tolerance=1e-6):
    """Calculate implied volatility using Newton-Raphson method."""
    if T <= 0:
        return 0.0

    # Initial guess using Brenner-Subrahmanyam approximation
    sigma = math.sqrt(2 * math.pi / T) * market_price / S
    sigma = max(0.01, min(sigma, 5.0))

    for _ in range(max_iterations):
        price = option_price(S, K, T, r, sigma, option_type)
        diff = price - market_price

        if abs(diff) < tolerance:
            return sigma

        v = vega(S, K, T, r, sigma) * 100  # vega is per 1%, need per 100%
        if abs(v) < 1e-10:
            break

        sigma -= diff / v
        sigma = max(0.001, min(sigma, 5.0))

    return sigma


def greeks(S, K, T, r=RISK_FREE_RATE, sigma=0.2, option_type="CE"):
    """Calculate all Greeks for an option."""
    return {
        "price": option_price(S, K, T, r, sigma, option_type),
        "delta": delta(S, K, T, r, sigma, option_type),
        "gamma": gamma(S, K, T, r, sigma),
        "theta": theta(S, K, T, r, sigma, option_type),
        "vega": vega(S, K, T, r, sigma),
        "iv": sigma,
    }


def probability_otm(S, K, T, r=RISK_FREE_RATE, sigma=0.2, option_type="CE"):
    """Probability that option expires OTM."""
    if T <= 0:
        if option_type == "CE":
            return 0.0 if S > K else 1.0
        return 0.0 if S < K else 1.0
    _d2 = d2(S, K, T, r, sigma)
    if option_type == "CE":
        return norm.cdf(-_d2)
    return norm.cdf(_d2)
