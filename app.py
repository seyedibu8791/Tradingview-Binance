from flask import Flask, request, jsonify
import requests, hmac, hashlib, time, threading, os
from config import *

app = Flask(__name__)

# ===== Binance Signed Request =====
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

# ===== Leverage & Margin =====
def set_leverage_and_margin(symbol):
    try:
        binance_signed_request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": LEVERAGE})
        binance_signed_request("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": MARGIN_TYPE})
    except Exception as e:
        print("⚠️ Leverage/Margin setup failed:", e)

# ===== Quantity Calculation =====
def calculate_quantity(symbol, usdt_value):
    try:
        price_data = requests.get(f"{BASE_URL}/fapi/v1/ticker/price", params={"symbol": symbol}).json()
        price = float(price_data["price"])
        qty = round(usdt_value / price, 3)
        return qty
    except:
        return 0.001

# ===== Check Position =====
def get_open_position(symbol):
    positions = binance_signed_request("GET", "/fapi/v2/positionRisk")
    for pos in positions:
        if pos["symbol"] == symbol:
            position_amt = float(pos["positionAmt"])
            if position_amt != 0:
                return position_amt
    return 0

# ===== Open Position =====
def open_position(symbol, side):
    set_leverage_and_margin(symbol)
    qty = calculate_quantity(symbol, TRADE_AMOUNT)
    r = binance_signed_request("POST", "/fapi/v1/order", {
        "symbol": symbol,
        "side": side,
        "type": "MARKET",
        "quantity": qty
    })
    print(f"✅ ENTRY: {side} {symbol}, Qty: {qty}")
    return r

# ===== Close Position =====
def close_position(symbol, side, price):
    position_amt = get_open_position(symbol)
    if position_amt == 0:
        print(f"⚠️ No open position for {symbol}. Exit skipped.")
        return {"error": "No open position"}

    close_side = "SELL" if position_amt > 0 else "BUY"
    qty = abs(position_amt)

    if EXIT_ORDER_TYPE == "MARKET":
        delay = int(os.getenv("EXIT_ORDER_DELAY", "0"))
        print(f"⏱ Waiting {delay}s before MARKET exit...")
        time.sleep(delay)
        order = binance_signed_request("POST", "/fapi/v1/order", {
            "symbol": symbol,
            "side": close_side,
            "type": "MARKET",
            "quantity": qty
        })
    else:
        order = binance_signed_request("POST", "/fapi/v1/order", {
            "symbol": symbol,
            "side": close_side,
            "type": "LIMIT",
            "price": price,
            "quantity": qty,
            "timeInForce": "GTC"
        })

    print(f"✖️ EXIT {close_side}: {symbol} @ {price} Qty: {qty}")
    return order

# ===== Webhook =====
@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_data(as_text=True)
    try:
        ticker, comment, close_price, interval = data.split('|')
        symbol = ticker.upper().replace("USDT", "") + "USDT"
        close_price = float(close_price)

        print(f"📩 ALERT: {symbol} | {comment} | Price {close_price}")

        if "EXIT_LONG" in comment:
            r = close_position(symbol, "BUY", close_price)
        elif "EXIT_SHORT" in comment:
            r = close_position(symbol, "SELL", close_price)
        elif "LONG" in comment:
            r = open_position(symbol, "BUY")
        elif "SHORT" in comment:
            r = open_position(symbol, "SELL")
        else:
            r = {"error": "Unknown comment"}

        return jsonify({"status": "ok", "response": r})
    except Exception as e:
        print("❌ Error in webhook:", e)
        return jsonify({"error": str(e)})

# ===== Ping =====
@app.route('/ping', methods=['GET'])
def ping():
    return "pong", 200

# ===== Self-Ping =====
PING_INTERVAL = 300  # 5 min
def self_ping():
    while True:
        try:
            print("🔄 Self-ping to keep bot alive...")
            requests.get(f"https://tradingview-binance-2o1v.onrender.com/ping")
        except Exception as e:
            print("❌ Self-ping failed:", e)
        time.sleep(PING_INTERVAL)

threading.Thread(target=self_ping, daemon=True).start()

if __name__ == '__main__':
    port = int(os.getenv("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
