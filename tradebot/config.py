"""Shared project configuration."""

WATCHLIST = [
    "SPY", "QQQ", "GOOGL", "TSLA", "BE", "IONQ",
    "NVDA", "AAPL", "AMD", "META", "AMZN",
    "MSFT", "COIN", "PLTR", "SMCI", "IWM", "USO",
]

# Two-tier options-liquidity classification. Index ETFs and true
# mega-caps carry deep weekly chains (tight markets, real open interest
# at every strike); the rest of the watchlist often doesn't — a single
# global liquidity floor makes the contract-selection gate look broken
# on the high-beta names specifically, not because the gate is wrong but
# because the floor was calibrated for the wrong symbol class. This is a
# judgment call, not a vendor-provided classification — SPY/QQQ/IWM are
# unambiguous (index ETFs); AAPL/MSFT/AMZN/GOOGL/META/NVDA/TSLA are
# unambiguous (mega-cap, consistently deep weekly options volume).
# Everything else defaults to the thinner class.
DEEP_LIQUIDITY_SYMBOLS = frozenset({
    "SPY", "QQQ", "IWM", "AAPL", "MSFT", "AMZN", "GOOGL", "META", "NVDA", "TSLA",
})


def liquidity_class(symbol: str) -> str:
    return "deep" if symbol in DEEP_LIQUIDITY_SYMBOLS else "thin"
