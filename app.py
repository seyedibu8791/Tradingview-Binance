from flask import Flask, request, jsonify
import requests, hmac, hashlib, time, threading, os
from config import *

app = Flask(__name__)

# ===== Env / Exit Limit Settings =====
USE_BAR_EXIT = os.getenv("USE_BAR_HIGH_LOW_EXIT", "True").lower() in ("1", "true", "yes")
EXIT_WAIT_LIMIT_SECS = int(os.getenv("EXIT_WAIT_LIMIT_SECS", "5"))
OPPOSITE_CLOSE_DELAY = int(os.getenv("OPPOSITE_CLOSE_DELAY", "3"))  # ⏳ Delay (in seconds) before opening new position

# ===== Binance Helpers =====
def binance_signed_request(http_method, path, params=None):
    if params is None:
        params = {}
    params["timestamp"] = int(time.time() * 1000)
    query = "&".join([f"{k}={v}" for k, v in params.items()])
    signature = hmac.new(BINANCE_SECRET_KEY.encode(), query.encode(), hashlib.sha256).hexdigest()
    query += f"&signature={signature}"
    url = f"{BASE_URL}{path}?{query}"
    headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
    try:
        if http_method == "POST":
            return requests.post(url, headers=headers).json()
        elif http_method == "DELETE":
            return requests.delete(url, headers=headers).json()
        else:
            return requests.get(url, headers=headers).json()
    except Exception as e:
        print("❌ Binance request failed:", e)
        return {"error": str(e)}

def set_leverage_and_margin(symbol):
    try:
        binance_signed_request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": LEVERAGE})
        binance_signed_request("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": MARGIN_TYPE})
    except Exception as e:
        print("❌ Failed to set leverage/margin:", e)

# ===== Symbol Info =====
SYMBOL_INFO_CACHE = {}
OPEN_LIMIT_ORDERS = {}
EXIT_LIMIT_ORDERS = {}
EXIT_MONITORS = {}
EXIT_LOCK = threading.Lock()

def get_symbol_info(symbol):
    if symbol in SYMBOL_INFO_CACHE:
        return SYMBOL_INFO_CACHE[symbol]
    info = requests.get(f"{BASE_URL}/fapi/v1/exchangeInfo").json()
    for s in info.get("symbols", []):
        if s["symbol"] == symbol:
            SYMBOL_INFO_CACHE[symbol] = s
            return s
    return None

def round_quantity(symbol, qty):
    info = get_symbol_info(symbol)
    if not info:
        return round(qty, 3)
    step_size = float([f["stepSize"] for f in info["filters"] if f["filterType"] == "LOT_SIZE"][0])
    min_qty = float([f["minQty"] for f in info["filters"] if f["filterType"] == "LOT_SIZE"][0])
    qty = (qty // step_size) * step_size
    if qty < min_qty:
        qty = min_qty
    return round(qty, 8)

# ===== Active Trades =====
def count_active_trades():
    try:
        positions = binance_signed_request("GET", "/fapi/v2/positionRisk")
        active_positions = [p for p in positions if abs(float(p["positionAmt"])) > 0]
        return len(active_positions)
    except Exception as e:
        print("❌ Failed to fetch active trades:", e)
        return 0

# ===== Order Execution =====
def calculate_quantity(symbol):
    try:
        price_data = requests.get(f"{BASE_URL}/fapi/v1/ticker/price", params={"symbol": symbol}).json()
        price = float(price_data["price"])
        position_value = TRADE_AMOUNT * LEVERAGE
        qty = position_value / price
        qty = round_quantity(symbol, qty)
        return qty
    except Exception as e:
        print("❌ Failed to calculate quantity:", e)
        return 0.001

def execute_market_exit(symbol, side):
    pos_data = binance_signed_request("GET", "/fapi/v2/positionRisk", {"symbol": symbol})
    if not pos_data or abs(float(pos_data[0]["positionAmt"])) == 0:
        print(f"⚠️ No open position for {symbol}, skipping exit.")
        return {"status": "no_position"}

    qty = abs(float(pos_data[0]["positionAmt"]))
    qty = round_quantity(symbol, qty)
    close_side = "SELL" if side == "BUY" else "BUY"

    response = binance_signed_request("POST", "/fapi/v1/order", {
        "symbol": symbol,
        "side": close_side,
        "type": "MARKET",
        "quantity": qty
    })
    print(f"✖️ {close_side} MARKET EXIT executed for {symbol}, Qty: {qty}")
    return response

def close_existing_if_opposite(symbol, new_side):
    """If an opposite position exists, close it first before opening new."""
    try:
        pos_data = binance_signed_request("GET", "/fapi/v2/positionRisk", {"symbol": symbol})
        if not pos_data:
            return False
        amt = float(pos_data[0]["positionAmt"])
        if amt > 0 and new_side == "SELL":
            print(f"🔁 Opposite signal detected — closing LONG before new SHORT for {symbol}")
            execute_market_exit(symbol, "BUY")
            print(f"⏳ Waiting {OPPOSITE_CLOSE_DELAY}s before opening new SHORT...")
            time.sleep(OPPOSITE_CLOSE_DELAY)
            return True
        elif amt < 0 and new_side == "BUY":
            print(f"🔁 Opposite signal detected — closing SHORT before new LONG for {symbol}")
            execute_market_exit(symbol, "SELL")
            print(f"⏳ Waiting {OPPOSITE_CLOSE_DELAY}s before opening new LONG...")
            time.sleep(OPPOSITE_CLOSE_DELAY)
            return True
        return False
    except Exception as e:
        print("❌ Failed to close opposite position:", e)
        return False

def open_position(symbol, side, limit_price):
    active_count = count_active_trades()
    if active_count >= MAX_ACTIVE_TRADES:
        print(f"🚫 Trade limit reached ({active_count}/{MAX_ACTIVE_TRADES}). Skipping {symbol} {side}.")
        return {"status": "max_trades_reached", "active_trades": active_count}

    close_existing_if_opposite(symbol, side)
    set_leverage_and_margin(symbol)
    qty = calculate_quantity(symbol)

    response = binance_signed_request("POST", "/fapi/v1/order", {
        "symbol": symbol,
        "side": side,
        "type": "LIMIT",
        "timeInForce": "GTC",
        "quantity": qty,
        "price": limit_price
    })
    if "orderId" in response:
        OPEN_LIMIT_ORDERS[symbol] = response["orderId"]
        print(f"📊 {side} LIMIT ENTRY: {symbol}, Qty: {qty}, Price: {limit_price}")
    else:
        print(f"❌ Entry failed for {symbol}: {response}")
    return response

# ===== Webhook =====
@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_data(as_text=True)
    try:
        parts = [p.strip() for p in data.split('|')]
        if len(parts) >= 6:
            ticker, comment, close_price, bar_high, bar_low, interval = parts[:6]
        else:
            ticker, comment, close_price, interval = parts[0], parts[1], parts[2], parts[-1]
            bar_high = bar_low = None

        symbol = ticker.replace("USDT", "") + "USDT"
        close_price = float(close_price)
        bar_high = float(bar_high) if bar_high else None
        bar_low = float(bar_low) if bar_low else None

        # 🔹 Handle signal types
        if comment in ["BUY_ENTRY", "CROSS_EXIT_SHORT"]:
            close_existing_if_opposite(symbol, "BUY")
            r = open_position(symbol, "BUY", close_price)

        elif comment in ["SELL_ENTRY", "CROSS_EXIT_LONG"]:
            close_existing_if_opposite(symbol, "SELL")
            r = open_position(symbol, "SELL", close_price)

        elif comment == "EXIT_LONG":
            r = execute_market_exit(symbol, "BUY")

        elif comment == "EXIT_SHORT":
            r = execute_market_exit(symbol, "SELL")

        else:
            r = {"error": f"Unknown comment: {comment}"}

        return jsonify({"status": "ok", "response": r})

    except Exception as e:
        print("❌ Webhook Error:", e)
        return jsonify({"error": str(e)})

# ===== Ping =====
@app.route('/ping', methods=['GET'])
def ping():
    return "pong", 200

# ===== Self Ping =====
PING_INTERVAL = 5 * 60
def self_ping():
    while True:
        try:
            print("🔄 Self-ping to keep bot alive...")
            requests.get(f"https://tradingview-binance-2o1v.onrender.com/ping")
        except Exception as e:
            print("❌ Self-ping failed:", e)
        time.sleep(PING_INTERVAL)

threading.Thread(target=self_ping, daemon=True).start()

# ===== Run Flask =====
if __name__ == '__main__':
    port = int(os.getenv("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
