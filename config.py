import os

# ==============================
# 🔹 BINANCE CONFIGURATION
# ==============================
USE_TESTNET = os.getenv("USE_TESTNET", "True") == "True"

TESTNET_API_KEY    = os.getenv("TESTNET_API_KEY")
TESTNET_SECRET_KEY = os.getenv("TESTNET_SECRET_KEY")
LIVE_API_KEY       = os.getenv("LIVE_API_KEY")
LIVE_SECRET_KEY    = os.getenv("LIVE_SECRET_KEY")

TESTNET_BASE_URL   = "https://testnet.binancefuture.com"
LIVE_BASE_URL      = "https://fapi.binance.com"

if USE_TESTNET:
    BINANCE_API_KEY    = TESTNET_API_KEY
    BINANCE_SECRET_KEY = TESTNET_SECRET_KEY
    BASE_URL           = TESTNET_BASE_URL
else:
    BINANCE_API_KEY    = LIVE_API_KEY
    BINANCE_SECRET_KEY = LIVE_SECRET_KEY
    BASE_URL           = LIVE_BASE_URL

# ==============================
# 🔹 TRADING PARAMETERS
# ==============================
TRADE_AMOUNT = float(os.getenv("TRADE_AMOUNT", "50"))   # USD per trade
LEVERAGE     = int(os.getenv("LEVERAGE", "20"))         # Leverage multiplier
MARGIN_TYPE  = os.getenv("MARGIN_TYPE", "CROSS")        # CROSS or ISOLATED

# ==============================
# 🔹 EXIT ORDER PARAMETERS
# ==============================
EXIT_MARKET_DELAY = int(os.getenv("EXIT_MARKET_DELAY", "60"))  # delay before market exit (in seconds)
