from flask import Flask, request, jsonify
import requests, hmac, hashlib, time, threading, os
from config import *

app = Flask(__name__)

# ===== Binance Request Helper =====
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

# ===== Setup Leverage & Margin =====
def set_leverage_and_margin(symbol):
    try:
        binance_signed_request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": LEVERAGE})
        binance_signed_request("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": MARGIN_TYPE})
    except Exception as e:
        print("⚠️ Leverage/Margin setup failed:", e)

# ===== Get Precision =====
def get_symbol_precision(symbol):
    try:
        info = requests.get(f"{BASE_URL}/fapi/v1/exchangeInfo").json()
        for s in info["symbols"]:
            if s["symbol"] == symbol:
                tick_size = float([f["tickSize"] for f in s["filters"] if f["filterType"] == "PRICE_FILTER"][0])
                return tick_size
    except Exception as e:
        print("⚠️ Precision fetch failed:", e)
    return 0.01

# ===== Calculate Quantity =====
def calculate_quantity(symbol, usdt_value):
    try:
        price_data = requests.get(f"{BASE_URL}/fapi/v1/ticker/price", params={"symbol": symbol}).json()
        price = float(price_data["price"])
        qty = round(usdt_value / price, 3)
        return qty
    except:
        return 0.001

# ===== Order Execution =====
def open_position(symbol, side):
    set_leverage_and_margin(symbol)
    qty = calculate_quantity(symbol, TRADE_AMOUNT)

    # ---- Market Entry ----
    entry = binance_signed_request("POST", "/fapi/v1/order", {
        "symbol": symbol,
        "side": side,
        "type": "MARKET",
        "quantity": qty
    })

    if "orderId" not in entry:
        print("❌ Entry failed:", entry)
        return entry

    print(f"✅ {side} ENTRY: {symbol}, Qty: {qty}")

    # ---- Get Filled Price ----
    time.sleep(1)
    trades = binance_signed_request("GET", "/fapi/v1/userTrades", {"symbol": symbol, "limit": 1})
    entry_price = float(trades[0]["price"]) if trades else 0

    # ---- Get pip correction ----
    tick_size = get_symbol_precision(symbol)
    pips_correction = 1 / tick_size if tick_size > 0 else 1

    # ---- Calculate SL and Trailing ----
    if side == "BUY":
        stop_price = round(entry_price * (1 - STOP_LOSS_PCT / 100), 2)
    else:
        stop_price = round(entry_price * (1 + STOP_LOSS_PCT / 100), 2)

    print(f"📉 Setting SL @ {stop_price}, Trail Activation={TRAIL_ACTIVATION}%, Offset={TRAIL_OFFSET}%")

    # ---- Stop Loss Order ----
    stop_order = binance_signed_request("POST", "/fapi/v1/order", {
        "symbol": symbol,
        "side": "SELL" if side == "BUY" else "BUY",
        "type": "STOP_MARKET",
        "stopPrice": stop_price,
        "closePosition": "true"
    })

    # ---- Trailing Stop Order ----
    trail_order = binance_signed_request("POST", "/fapi/v1/order", {
        "symbol": symbol,
        "side": "SELL" if side == "BUY" else "BUY",
        "type": "TRAILING_STOP_MARKET",
        "callbackRate": TRAIL_OFFSET,
        "activationPrice": round(entry_price * (1 + TRAIL_ACTIVATION / 100), 2) if side == "BUY" else round(entry_price * (1 - TRAIL_ACTIVATION / 100), 2),
        "closePosition": "true"
    })

    print("✅ Stop-loss and trailing stop placed.")
    return {"entry": entry, "stop": stop_order, "trail": trail_order}

# ===== Webhook Endpoint =====
@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_data(as_text=True)
    try:
        ticker, comment, close_price, interval = data.split('|')
        symbol = ticker.replace("USDT", "") + "USDT"

        if "LONG" in comment.upper():
            r = open_position(symbol, "BUY")
        elif "SHORT" in comment.upper():
            r = open_position(symbol, "SELL")
        else:
            r = {"error": "Invalid signal comment"}

        return jsonify({"status": "ok", "response": r})

    except Exception as e:
        return jsonify({"error": str(e)})

# ===== Ping Endpoint =====
@app.route('/ping', methods=['GET'])
def ping():
    return "pong", 200

# ===== Self Ping =====
PING_INTERVAL = 5 * 60
def self_ping():
    while True:
        try:
            print("🔄 Self-ping to keep bot alive...")
            requests.get(f"https://your-render-app-name.onrender.com/ping")
        except Exception as e:
            print("❌ Self-ping failed:", e)
        time.sleep(PING_INTERVAL)

threading.Thread(target=self_ping, daemon=True).start()

# ===== Run Flask =====
if __name__ == '__main__':
    port = int(os.getenv("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
