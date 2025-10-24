# ==============================
# 🔹 BINANCE CONFIGURATION
# ==============================

# Toggle this to switch between TESTNET and LIVE
USE_TESTNET = True  # ✅ True = Binance Futures Testnet | False = Live Binance Futures

# --- Testnet Keys and URL ---
TESTNET_API_KEY    = "KaKPt1FAC4prWnhPgbMWRqgvGcAON5EyHB4V0RoZhPr9esSg4zgFCqFl6ZX5NR5A"
TESTNET_SECRET_KEY = "0EHstzo6QumDXmysHPLEzn8ejfYznmC5a8w36Oq572go07SrhbE4xGwnVMwpCKyP"
TESTNET_BASE_URL   = "https://testnet.binancefuture.com"

# --- Live Keys and URL ---
LIVE_API_KEY       = "your_live_api_key"
LIVE_SECRET_KEY    = "your_live_secret_key"
LIVE_BASE_URL      = "https://fapi.binance.com"

# ==============================
# 🔹 TRADING PARAMETERS
# ==============================
TRADE_AMOUNT = 10           # USDT per trade
LEVERAGE     = 20           # Leverage to use
MARGIN_TYPE  = "ISOLATED"   # ISOLATED or CROSSED

# ==============================
# 🔹 AUTO-SELECTION LOGIC
# ==============================
if USE_TESTNET:
    BINANCE_API_KEY    = TESTNET_API_KEY
    BINANCE_SECRET_KEY = TESTNET_SECRET_KEY
    BASE_URL           = TESTNET_BASE_URL
else:
    BINANCE_API_KEY    = LIVE_API_KEY
    BINANCE_SECRET_KEY = LIVE_SECRET_KEY
    BASE_URL           = LIVE_BASE_URL
