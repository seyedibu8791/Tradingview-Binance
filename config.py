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
TRADE_AMOUNT        = float(os.getenv("TRADE_AMOUNT", "50"))       # $ per trade
LEVERAGE            = int(os.getenv("LEVERAGE", "20"))             # leverage
MARGIN_TYPE         = os.getenv("MARGIN_TYPE", "ISOLATED")         # ISOLATED or CROSS

# ==============================
# 🔹 TRAILING STOP SETTINGS
# ==============================
TRAIL_ACTIVATION    = float(os.getenv("TRAIL_ACTIVATION", "0.8")) # % activation
TRAIL_OFFSET_LOW    = float(os.getenv("TRAIL_OFFSET_LOW", "0.1"))  # min offset %
TRAIL_OFFSET_HIGH   = float(os.getenv("TRAIL_OFFSET_HIGH", "0.1")) # max offset %

# ==============================
# 🔹 STOPLOSS SETTINGS
# ==============================
STOPLOSS_PERCENT    = float(os.getenv("STOPLOSS_PERCENT", "3.0")) # % stoploss

# ==============================
# 🔹 BOT SETTINGS
# ==============================
PING_INTERVAL       = int(os.getenv("PING_INTERVAL", 300))         # seconds
TS_UPDATE_INTERVAL  = int(os.getenv("TS_UPDATE_INTERVAL", 5))      # seconds
