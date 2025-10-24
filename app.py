from flask import Flask, request, jsonify
import requests, hmac, hashlib, time, threading, os
from config import *

app = Flask(__name__)

# ======================
# 🔹 Binance Utilities
# ======================
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
    except:
        pass

def calculate_quantity(symbol, usdt_value):
    try:
        price_data = requests.get(f"{BASE_URL}/fapi/v1/ticker/price", params={"symbol": symbol}).json()
        price = float(price_data["price"])
        qty = round(usdt_value / price, 3)
        return qty
    except:
        return 0.001

# ======================
# 🔹 Position Management
# ======================
def open_position(symbol, side):
    set_leverage_and_margin(symbol)
    qty = calculate_quantity(symbol, TRADE_AMOUNT)

    # Step 1: Market entry
    entry = binance_signed_request("POST", "/fapi/v1/order", {
        "symbol": symbol,
        "side": side,
        "type": "MARKET",
        "quantity": qty
    })
    print(f"📈 {side} ENTRY: {symbol}, Qty: {qty}")

    # Wait briefly for position confirmation
    time.sleep(2)

    # Step 2: Fetch entry price
    price_data = requests.get(f"{BASE_URL}/fapi/v1/ticker/price", params={"symbol": symbol}).json()
    entry_price = float(price_data["price"])

    # Step 3: Determine exit side
    exit_side = "SELL" if side == "BUY" else "BUY"

    # Step 4: Compute stop-loss and trailing params
    stop_loss_price = entry_price * (1 - STOP_LOSS_PERCENT / 100) if side == "BUY" else entry_price * (1 + STOP_LOSS_PERCENT / 100)
    activation_price = entry_price * (1 - TRAIL_ACTIVATION_PERCENT / 100) if side == "BUY" else entry_price * (1 + TRAIL_ACTIVATION_PERCENT / 100)
    callback_rate = TRAIL_OFFSET_PERCENT

    # Step 5: Place Stop-Loss order
    sl_order = binance_signed_request("POST", "/fapi/v1/order", {
        "symbol": symbol,
        "side": exit_side,
        "type": "STOP_MARKET",
        "stopPrice": round(stop_loss_price, 2),
        "reduceOnly": True,
        "quantity": qty,
        "timeInForce": "GTC"
    })
    print(f"🛑 STOP LOSS set @ {round(stop_loss_price, 2)}")

    # Step 6: Place Trailing Stop
    trail_order = binance_signed_request("POST", "/fapi/v1/order", {
        "symbol": symbol,
        "side": exit_side,
        "type": "TRAILING_STOP_MARKET",
        "callbackRate": callback_rate,
        "activationPrice": round(activation_price, 2),
        "reduceOnly": True,
        "quantity": qty
    })
    print(f"📉 TRAILING STOP set @ {round(activation_price, 2)} ({callback_rate}%)")

    return {"entry": entry, "stop_loss": sl_order, "trailing": trail_order}


# ======================
# 🔹 Webhook Endpoint
# ======================
@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_data(as_text=True)
    try:
        ticker, comment, close_price, interval = data.split('|')
        symbol = ticker.replace("USDT", "") + "USDT"

        # Determine entry direction
        if "LONG" in comment and "EXIT" not in comment:
            r = open_position(symbol, "BUY")
        elif "SHORT" in comment and "EXIT" not in comment:
            r = open_position(symbol, "SELL")
        else:
            # Ignore exit signals since trailing stop & SL now handle exits
            print(f"⚠️ Ignored signal for {symbol} ({comment})")
            r = {"ignored": comment}

        return jsonify({"status": "ok", "response": r})

    except Exception as e:
        return jsonify({"error": str(e)})


# ======================
# 🔹 Ping & Self-Ping
# ======================
@app.route('/ping', methods=['GET'])
def ping():
    return "pong", 200

PING_INTERVAL = 5 * 60  # 5 minutes

def self_ping():
    while True:
        try:
            print("🔄 Self-ping to keep bot alive...")
            requests.get(f"https://tradingview-binance-2o1v.onrender.com/ping")
        except Exception as e:
            print("❌ Self-ping failed:", e)
        time.sleep(PING_INTERVAL)

threading.Thread(target=self_ping, daemon=True).start()

# ======================
# 🔹 Run Server
# ======================
if __name__ == '__main__':
    port = int(os.getenv("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
