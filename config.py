import os

# ==============================
# 🔹 BINANCE CONFIGURATION
# ==============================
USE_TESTNET = os.getenv("USE_TESTNET", "True") == "True"

TESTNET_API_KEY    = os.getenv("TESTNET_API_KEY")
TESTNET_SECRET_KEY = os.getenv("TESTNET_SECRET_KEY")
LIVE_API_KEY       = os.getenv("LIVE_API_KEY")
LIVE_SECRET_KEY    = os.getenv("LIVE_SECRET_KEY")

# Binance API endpoints
TESTNET_BASE_URL   = "https://testnet.binancefuture.com"
LIVE_BASE_URL      = "https://fapi.binance.com"

# Choose environment
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
TRADE_AMOUNT = float(os.getenv("TRADE_AMOUNT", "50"))   # USDT per trade
LEVERAGE     = int(os.getenv("LEVERAGE", "20"))         # leverage
MARGIN_TYPE  = os.getenv("MARGIN_TYPE", "ISOLATED")     # ISOLATED or CROSSED

# ==============================
# 🔹 EXIT ORDER CONFIGURATION
# ==============================
# Choose "MARKET" or "LIMIT" for exit type
EXIT_ORDER_TYPE = os.getenv("EXIT_ORDER_TYPE", "LIMIT").upper()

# Delay before executing MARKET exit (to capture max value)
# Applicable only if EXIT_ORDER_TYPE == "MARKET"
MARKET_EXIT_DELAY = int(os.getenv("MARKET_EXIT_DELAY", "2"))  # seconds

# Timeout for pending exit order
EXIT_TIMEOUT = int(os.getenv("EXIT_TIMEOUT", "05"))  # seconds to wait before cancel or force market exit

# ==============================
# 🔹 SYSTEM SETTINGS
# ==============================
PING_INTERVAL = int(os.getenv("PING_INTERVAL", "300"))  # seconds (default: 5 mins)

# App domain for self-ping (update to your Render or VPS URL)
APP_URL = os.getenv("APP_URL", "https://tradingview-binance-2o1v.onrender.com")
