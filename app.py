from flask import Flask, request, jsonify
import requests, hmac, hashlib, time, threading, os

from config import *

app = Flask(__name__)

# =========================
# BINANCE HELPERS
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
    if http_method == "POST":
        return requests.post(url, headers=headers).json()
    elif http_method == "DELETE":
        return requests.delete(url, headers=headers).json()
    else:
        return requests.get(url, headers=headers).json()

def set_leverage_and_margin(symbol):
    try:
        binance_signed_request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": LEVERAGE})
        binance_signed_request("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": MARGIN_TYPE})
    except Exception as e:
        print("Set leverage/margin error:", e)

def calculate_quantity(symbol, usdt_value):
    try:
        price_data = requests.get(f"{BASE_URL}/fapi/v1/ticker/price", params={"symbol": symbol}).json()
        price = float(price_data["price"])
        qty = round(usdt_value / price, 3)
        return qty
    except:
        return 0.001

# =========================
# ORDER EXECUTION
# =========================
open_positions = {}  # Track current open positions {symbol: side}
trailing_orders = {}  # Track trailing orders {symbol: callbackRate}

def open_position(symbol, side):
    set_leverage_and_margin(symbol)
    qty = calculate_quantity(symbol, TRADE_AMOUNT)
    r = binance_signed_request("POST", "/fapi/v1/order", {
        "symbol": symbol,
        "side": side,
        "type": "MARKET",
        "quantity": qty
    })
    open_positions[symbol] = side
    print(f"📊 ENTRY: {side} {symbol}, Qty: {qty}")
    # Place initial stop loss
    place_stop_loss(symbol, side)
    # Place initial trailing stop
    place_trailing_stop(symbol, side)
    return r

def place_stop_loss(symbol, side):
    stop_price = None
    qty = calculate_quantity(symbol, TRADE_AMOUNT)
    if side == "BUY":
        stop_price = float(requests.get(f"{BASE_URL}/fapi/v1/ticker/price", params={"symbol": symbol}).json()["price"]) * (1 - STOP_LOSS / 100)
        stop_side = "SELL"
    else:
        stop_price = float(requests.get(f"{BASE_URL}/fapi/v1/ticker/price", params={"symbol": symbol}).json()["price"]) * (1 + STOP_LOSS / 100)
        stop_side = "BUY"
    r = binance_signed_request("POST", "/fapi/v1/order", {
        "symbol": symbol,
        "side": stop_side,
        "type": "STOP_MARKET",
        "stopPrice": round(stop_price, 2),
        "quantity": qty
    })
    print(f"🛑 STOP LOSS placed for {symbol} at {round(stop_price,2)}")
    return r

def place_trailing_stop(symbol, side):
    qty = calculate_quantity(symbol, TRADE_AMOUNT)
    # Use env vars for trailing activation and callback
    callbackRate = TRAIL_OFFSET  # %
    activationPrice = TRAIL_ACTIVATION  # %
    if side == "BUY":
        stop_side = "SELL"
    else:
        stop_side = "BUY"
    r = binance_signed_request("POST", "/fapi/v1/order", {
        "symbol": symbol,
        "side": stop_side,
        "type": "TRAILING_STOP_MARKET",
        "quantity": qty,
        "callbackRate": callbackRate,
        "activationPrice": None  # Let Binance calculate activation from market price
    })
    trailing_orders[symbol] = callbackRate
    print(f"🏹 TRAILING STOP placed for {symbol} with callback {callbackRate}%")
    return r

def update_trailing_stop(symbol, side, new_callback):
    """
    Cancel previous trailing stop & place new one if callbackRate changes
    """
    # Cancel old trailing stop
    binance_signed_request("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol})
    print(f"🔄 Cancelled old trailing stop for {symbol}")
    # Place new trailing stop
    place_trailing_stop(symbol, side)

# =========================
# WEBHOOK
# =========================
@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_data(as_text=True)
    try:
        ticker, comment, close_price, interval = data.split('|')
        symbol = ticker.replace("USDT","") + "USDT"
        close_price = float(close_price)

        if "LONG" in comment:
            r = open_position(symbol, "BUY")
        elif "SHORT" in comment:
            r = open_position(symbol, "SELL")
        else:
            r = {"error":"Unknown comment or ignored exit"}
        return jsonify({"status": "ok", "response": r})

    except Exception as e:
        return jsonify({"error": str(e)})

# =========================
# PING & SELF-PING
# =========================
@app.route('/ping', methods=['GET'])
def ping():
    return jsonify({"status":"ok", "message":"Bot is alive"}), 200

def self_ping():
    while True:
        try:
            requests.get(os.getenv("RENDER_EXTERNAL_URL","http://localhost:5000")+"/ping")
            print("[Self-ping] Ping sent successfully.")
        except Exception as e:
            print("[Self-ping] Error:", e)
        time.sleep(600)  # 10 minutes

threading.Thread(target=self_ping, daemon=True).start()

# =========================
# RUN
# =========================
if __name__ == "__main__":
    port = int(os.getenv("PORT",5000))
    app.run(host='0.0.0.0', port=port)
