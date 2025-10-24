from flask import Flask, request, jsonify
import requests, hmac, hashlib, time, threading
from config import *

app = Flask(__name__)

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
        print("Error setting leverage/margin:", e)

def calculate_quantity(symbol, usdt_value):
    try:
        price_data = requests.get(f"{BASE_URL}/fapi/v1/ticker/price", params={"symbol": symbol}).json()
        price = float(price_data["price"])
        qty = round(usdt_value / price, 3)
        return qty
    except:
        return 0.001

# ===== Trailing Stop Helpers =====
def ts_dynamic(profit_percent):
    delta = TRAIL_OFFSET_HIGH - TRAIL_OFFSET_LOW
    dynamic_ts = max((delta / 9.5) * (profit_percent - 0.5) + TRAIL_OFFSET_LOW, TRAIL_OFFSET_LOW)
    return round(dynamic_ts, 2)

# ===== Order Execution =====
def open_position(symbol, side):
    set_leverage_and_margin(symbol)
    qty = calculate_quantity(symbol, TRADE_AMOUNT)
    r = binance_signed_request("POST", "/fapi/v1/order", {
        "symbol": symbol,
        "side": side,
        "type": "MARKET",
        "quantity": qty
    })
    print(f"📊 {side} ENTRY: {symbol}, Qty: {qty}")
    return r

def set_trailing_stop(symbol, side, entry_price):
    # Binance trailing stop uses activation price and callback rate %
    if side.upper() == "BUY":
        stop_side = "SELL"
        activation_price = round(entry_price * (1 + TRAIL_ACTIVATION / 100), 2)
    else:
        stop_side = "BUY"
        activation_price = round(entry_price * (1 - TRAIL_ACTIVATION / 100), 2)
    
    qty = calculate_quantity(symbol, TRADE_AMOUNT)
    r = binance_signed_request("POST", "/fapi/v1/order", {
        "symbol": symbol,
        "side": stop_side,
        "type": "TRAILING_STOP_MARKET",
        "quantity": qty,
        "callbackRate": round(ts_dynamic(TRAIL_ACTIVATION), 2),
        "activationPrice": activation_price
    })
    print(f"🟡 Trailing Stop SET: {stop_side}, Entry: {entry_price}, Activation: {activation_price}, Qty: {qty}")
    return r

def set_stoploss(symbol, side, entry_price):
    qty = calculate_quantity(symbol, TRADE_AMOUNT)
    if side.upper() == "BUY":
        stop_side = "SELL"
        stop_price = round(entry_price * (1 - STOPLOSS_PERCENT / 100), 2)
    else:
        stop_side = "BUY"
        stop_price = round(entry_price * (1 + STOPLOSS_PERCENT / 100), 2)
    
    r = binance_signed_request("POST", "/fapi/v1/order", {
        "symbol": symbol,
        "side": stop_side,
        "type": "STOP_MARKET",
        "stopPrice": stop_price,
        "closePosition": True
    })
    print(f"🔴 Stoploss SET: {stop_side}, Entry: {entry_price}, SL Price: {stop_price}, Qty: {qty}")
    return r

# ===== Webhook Endpoint =====
@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_data(as_text=True)
    try:
        ticker, comment, close_price, interval = data.split('|')
        symbol = ticker.replace("USDT", "") + "USDT"
        close_price = float(close_price)
        side = None

        if "LONG" in comment and "EXIT" not in comment:
            side = "BUY"
        elif "SHORT" in comment and "EXIT" not in comment:
            side = "SELL"

        if side:
            open_position(symbol, side)
            set_trailing_stop(symbol, side, close_price)
            set_stoploss(symbol, side, close_price)
            return jsonify({"status": "ok", "message": f"{side} position opened with trailing stop & SL"})
        else:
            return jsonify({"status": "ignored", "message": "No entry signal found, exit ignored"})
    except Exception as e:
        return jsonify({"error": str(e)})

# ===== Ping Endpoint =====
@app.route('/ping', methods=['GET'])
def ping():
    return "pong", 200

# ===== Self-Ping Thread =====
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
