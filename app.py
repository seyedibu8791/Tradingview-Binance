# app.py

from flask import Flask, request, jsonify
import requests, hmac, hashlib, time, threading, os
from config import *
from trade_notifier import log_trade_entry, log_trade_exit  # Telegram notifications

app = Flask(__name__)

# =========================
# Binance Signed Request
# =========================
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

# =========================
# Leverage & Margin
# =========================
def set_leverage_and_margin(symbol):
    try:
        binance_signed_request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": LEVERAGE})
        binance_signed_request("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": MARGIN_TYPE})
    except Exception as e:
        print("❌ Failed to set leverage/margin:", e)

# =========================
# Symbol Info & Qty
# =========================
SYMBOL_INFO_CACHE = {}

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
    step_size = float([f["stepSize"] for f in info["filters"] if f["filterType"]=="LOT_SIZE"][0])
    min_qty = float([f["minQty"] for f in info["filters"] if f["filterType"]=="LOT_SIZE"][0])
    qty = (qty // step_size) * step_size
    if qty < min_qty:
        qty = min_qty
    return round(qty, 8)

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

# =========================
# Active Trades
# =========================
def count_active_trades():
    try:
        positions = binance_signed_request("GET", "/fapi/v2/positionRisk")
        active_positions = [p for p in positions if abs(float(p["positionAmt"])) > 0]
        return len(active_positions)
    except Exception as e:
        print("❌ Failed to fetch active trades:", e)
        return 0

# =========================
# Open Position
# =========================
def open_position(symbol, side, limit_price):
    active_count = count_active_trades()
    if active_count >= MAX_ACTIVE_TRADES:
        print(f"🚫 Max active trades reached ({active_count}/{MAX_ACTIVE_TRADES})")
        return {"status":"max_trades_reached"}

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
        threading.Thread(target=wait_and_notify_filled_entry, args=(symbol, side, response["orderId"]), daemon=True).start()

    return response

def wait_and_notify_filled_entry(symbol, side, order_id):
    """Wait until Binance fills the entry order, then notify Telegram."""
    while True:
        order_status = binance_signed_request("GET", "/fapi/v1/order", {"symbol": symbol, "orderId": order_id})
        if order_status.get("status") == "FILLED":
            filled_price = float(order_status["avgFillPrice"])
            log_trade_entry(symbol, side, order_id, filled_price)
            break
        time.sleep(0.5)

# =========================
# Exit Position
# =========================
def execute_market_exit(symbol, side):
    pos_data = binance_signed_request("GET", "/fapi/v2/positionRisk", {"symbol": symbol})
    if not pos_data or abs(float(pos_data[0]["positionAmt"])) == 0:
        return {"status":"no_position"}

    qty = abs(float(pos_data[0]["positionAmt"]))
    qty = round_quantity(symbol, qty)
    close_side = "SELL" if side=="BUY" else "BUY"

    response = binance_signed_request("POST", "/fapi/v1/order", {
        "symbol": symbol,
        "side": close_side,
        "type": "MARKET",
        "quantity": qty
    })

    if "orderId" in response:
        threading.Thread(target=wait_and_notify_filled_exit, args=(symbol, response["orderId"]), daemon=True).start()

    return response

def wait_and_notify_filled_exit(symbol, order_id):
    """Wait until Binance fills the exit order, then notify Telegram."""
    while True:
        order_status = binance_signed_request("GET", "/fapi/v1/order", {"symbol": symbol, "orderId": order_id})
        if order_status.get("status") == "FILLED" and "fills" in order_status:
            fills = order_status["fills"]
            avg_price = sum(float(f["price"])*float(f["qty"]) for f in fills) / sum(float(f["qty"]) for f in fills)
            log_trade_exit(symbol, order_id, avg_price)
            break
        time.sleep(0.5)

# =========================
# Async Exit & Open
# =========================
def async_exit_and_open(symbol, new_side, limit_price):
    def worker():
        pos_data = binance_signed_request("GET", "/fapi/v2/positionRisk", {"symbol": symbol})
        amt = float(pos_data[0]["positionAmt"]) if pos_data else 0
        opposite_side = None

        if amt>0 and new_side=="SELL":
            opposite_side="BUY"
        elif amt<0 and new_side=="BUY":
            opposite_side="SELL"

        if opposite_side:
            execute_market_exit(symbol, opposite_side)
            time.sleep(OPPOSITE_CLOSE_DELAY)

        open_position(symbol, new_side, limit_price)

    threading.Thread(target=worker, daemon=True).start()

# =========================
# Webhook
# =========================
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_data(as_text=True)
    try:
        parts = [p.strip() for p in data.split("|")]
        if len(parts) >= 6:
            ticker, comment, close_price, bar_high, bar_low, interval = parts[:6]
        else:
            ticker, comment, close_price, interval = parts[0], parts[1], parts[2], parts[-1]
            bar_high = bar_low = None

        symbol = ticker.replace("USDT","")+"USDT"
        close_price = float(close_price)

        if comment in ["BUY_ENTRY","CROSS_EXIT_SHORT"]:
            async_exit_and_open(symbol,"BUY",close_price)
        elif comment in ["SELL_ENTRY","CROSS_EXIT_LONG"]:
            async_exit_and_open(symbol,"SELL",close_price)
        elif comment=="EXIT_LONG":
            execute_market_exit(symbol,"BUY")
        elif comment=="EXIT_SHORT":
            execute_market_exit(symbol,"SELL")
        else:
            return jsonify({"error":f"Unknown comment: {comment}"})

        return jsonify({"status":"ok"})

    except Exception as e:
        print("❌ Webhook Error:", e)
        return jsonify({"error":str(e)})

# =========================
# Ping
# =========================
@app.route("/ping", methods=["GET"])
def ping():
    return "pong", 200

# =========================
# Self Ping (keep alive)
# =========================
def self_ping():
    while True:
        try:
            requests.get(f"https://tradingview-binance-2o1v.onrender.com/ping")
        except:
            pass
        time.sleep(5*60)

threading.Thread(target=self_ping, daemon=True).start()

# =========================
# Run Flask
# =========================
if __name__=="__main__":
    port = int(os.getenv("PORT",5000))
    app.run(host="0.0.0.0", port=port)
