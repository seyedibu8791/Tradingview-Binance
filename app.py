from flask import Flask, request, jsonify
import os, requests, time, hmac, hashlib
from urllib.parse import urlencode
from config import (
    BASE_URL, API_KEY, API_SECRET,
    MAX_ACTIVE_TRADES, EXIT_ORDER_TYPE, EXIT_DELAY_SEC
)

app = Flask(__name__)

# =========================
# UTILITY FUNCTIONS
# =========================

def sign_request(params):
    """Sign request parameters using HMAC SHA256"""
    query_string = urlencode(params)
    signature = hmac.new(
        API_SECRET.encode('utf-8'),
        query_string.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()
    return signature


def binance_request(method, endpoint, params=None):
    """Unified Binance REST request"""
    headers = {"X-MBX-APIKEY": API_KEY}
    url = f"{BASE_URL}{endpoint}"
    if not params:
        params = {}
    params['timestamp'] = int(time.time() * 1000)
    params['signature'] = sign_request(params)
    if method == "POST":
        res = requests.post(url, headers=headers, params=params)
    elif method == "GET":
        res = requests.get(url, headers=headers, params=params)
    elif method == "DELETE":
        res = requests.delete(url, headers=headers, params=params)
    else:
        raise Exception("Unsupported HTTP method")
    return res.json()


def get_active_trades():
    """Fetch number of open positions"""
    try:
        headers = {"X-MBX-APIKEY": API_KEY}
        url = f"{BASE_URL}/fapi/v2/positionRisk"
        response = requests.get(url, headers=headers)
        data = response.json()
        active_positions = [p for p in data if float(p['positionAmt']) != 0]
        return len(active_positions)
    except Exception as e:
        print("⚠️ Error fetching active trades:", e)
        return 0


def get_open_position(symbol):
    """Return open position info for a symbol if exists"""
    try:
        headers = {"X-MBX-APIKEY": API_KEY}
        url = f"{BASE_URL}/fapi/v2/positionRisk"
        response = requests.get(url, headers=headers)
        data = response.json()
        for pos in data:
            if pos["symbol"] == symbol and float(pos["positionAmt"]) != 0:
                return pos
        return None
    except Exception as e:
        print("⚠️ Error fetching position:", e)
        return None


def execute_order(symbol, side, quantity, order_type="MARKET", price=None):
    """Place a futures order"""
    params = {
        "symbol": symbol,
        "side": side,
        "type": order_type,
        "quantity": quantity,
    }
    if order_type == "LIMIT" and price:
        params["price"] = price
        params["timeInForce"] = "GTC"

    response = binance_request("POST", "/fapi/v1/order", params)
    print(f"🟢 Order Response: {response}")
    return response


# =========================
# MAIN ROUTE
# =========================
@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_json(force=True)
    if not data or "message" not in data:
        return jsonify({"status": "error", "message": "Invalid payload"}), 400

    message = data["message"]
    print(f"📩 Received: {message}")

    try:
        ticker, comment, price, interval = message.split("|")
        symbol = ticker.upper().replace("USDT", "") + "USDT"
        direction = comment.upper().strip()  # "LONG", "SHORT", "EXIT"
    except Exception as e:
        print("⚠️ Message parsing failed:", e)
        return jsonify({"status": "error", "message": "Invalid format"}), 400

    # ============ EXIT LOGIC ============
    if direction == "EXIT":
        position = get_open_position(symbol)
        if not position:
            print(f"❌ No open position found for {symbol}, skipping EXIT.")
            return jsonify({"status": "skipped", "reason": "no open position"})

        amt = abs(float(position["positionAmt"]))
        side = "SELL" if float(position["positionAmt"]) > 0 else "BUY"

        # Apply delay for MARKET exit
        if EXIT_ORDER_TYPE == "MARKET" and EXIT_DELAY_SEC > 0:
            print(f"⏳ Waiting {EXIT_DELAY_SEC}s before MARKET exit to capture better price...")
            time.sleep(EXIT_DELAY_SEC)

        print(f"🔻 Exiting {symbol} | Side={side} | Qty={amt} | Type={EXIT_ORDER_TYPE}")
        result = execute_order(symbol, side, amt, order_type=EXIT_ORDER_TYPE)
        return jsonify({"status": "exit_order_placed", "response": result})

    # ============ ENTRY LOGIC ============
    elif direction in ["LONG", "SHORT"]:
        # Check active trade count
        active_trades = get_active_trades()
        if active_trades >= MAX_ACTIVE_TRADES:
            print(f"⚠️ Max active trades ({MAX_ACTIVE_TRADES}) reached — skipping entry.")
            return jsonify({"status": "blocked", "reason": "max trades reached"})

        side = "BUY" if direction == "LONG" else "SELL"
        qty = 0.001  # example quantity (adjust per your config)
        print(f"🟢 Entry {symbol} | Side={side} | Qty={qty}")

        result = execute_order(symbol, side, qty)
        return jsonify({"status": "entry_order_placed", "response": result})

    else:
        print(f"⚠️ Unknown signal direction: {direction}")
        return jsonify({"status": "error", "message": "unknown direction"}), 400


# =========================
# RUN SERVER
# =========================
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv("PORT", 5000)))
