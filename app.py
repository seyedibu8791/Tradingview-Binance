from flask import Flask, request, jsonify
import requests, hmac, hashlib, time, threading, os
from config import *

app = Flask(__name__)

# ==========================
# 🔹 Binance API Helpers
# ==========================
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
    """Set leverage and margin type for the symbol."""
    try:
        binance_signed_request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": LEVERAGE})
        binance_signed_request("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": MARGIN_TYPE})
    except Exception as e:
        print(f"⚠️ Leverage/Margin setup failed: {e}")


def calculate_quantity(symbol, usdt_value):
    """Convert USDT value into trade quantity."""
    try:
        price_data = requests.get(f"{BASE_URL}/fapi/v1/ticker/price", params={"symbol": symbol}).json()
        price = float(price_data["price"])
        qty = round(usdt_value / price, 3)
        return qty
    except Exception as e:
        print("⚠️ Quantity calc failed:", e)
        return 0.001


def get_open_positions():
    """Return all open positions."""
    try:
        positions = binance_signed_request("GET", "/fapi/v2/positionRisk")
        return [p for p in positions if float(p["positionAmt"]) != 0]
    except Exception as e:
        print("⚠️ Could not fetch open positions:", e)
        return []


def get_open_position(symbol):
    """Return open position amount for a specific symbol."""
    try:
        for pos in get_open_positions():
            if pos["symbol"] == symbol:
                return float(pos["positionAmt"])
    except Exception as e:
        print(f"⚠️ Could not fetch position for {symbol}: {e}")
    return 0.0


def cancel_open_orders(symbol):
    """Cancel all open orders for the given symbol."""
    try:
        r = binance_signed_request("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol})
        print(f"🧹 Cancelled open orders for {symbol}")
        return r
    except Exception as e:
        print(f"⚠️ Cancel orders failed for {symbol}: {e}")
        return None


# ==========================
# 🔹 Order Execution
# ==========================
def open_position(symbol, side):
    """Open a long/short position."""
    open_positions = get_open_positions()
    if len(open_positions) >= MAX_ACTIVE_TRADES:
        print(f"🚫 Max active trades ({MAX_ACTIVE_TRADES}) reached. Skipping entry for {symbol}.")
        return {"status": "max_trades_reached"}

    existing_pos = get_open_position(symbol)
    if (side == "BUY" and existing_pos > 0) or (side == "SELL" and existing_pos < 0):
        print(f"⚠️ {symbol} already has position in same direction, skipping duplicate entry.")
        return {"status": "already_open"}

    set_leverage_and_margin(symbol)
    qty = calculate_quantity(symbol, TRADE_AMOUNT)
    r = binance_signed_request("POST", "/fapi/v1/order", {
        "symbol": symbol,
        "side": side,
        "type": "MARKET",
        "quantity": qty
    })
    print(f"✅ ENTRY {side}: {symbol}, Qty: {qty}")
    return r


def close_position(symbol, side, price):
    """Close position based on order type (LIMIT or MARKET)."""
    close_side = "SELL" if side == "BUY" else "BUY"
    qty = abs(get_open_position(symbol))
    if qty == 0:
        print(f"🚫 No open position to close for {symbol}. Cancelling orders...")
        cancel_open_orders(symbol)
        return {"status": "no_position"}

    order_type = os.getenv("EXIT_ORDER_TYPE", "LIMIT").upper()
    delay = int(os.getenv("EXIT_ORDER_DELAY", "2"))

    if order_type == "MARKET":
        print(f"🕒 Waiting {delay}s before MARKET exit to capture better price...")
        time.sleep(delay)
        params = {
            "symbol": symbol,
            "side": close_side,
            "type": "MARKET",
            "quantity": qty
        }
    else:
        params = {
            "symbol": symbol,
            "side": close_side,
            "type": "LIMIT",
            "price": price,
            "quantity": qty,
            "timeInForce": "GTC"
        }

    r = binance_signed_request("POST", "/fapi/v1/order", params)
    print(f"✖️ EXIT {close_side} {order_type}: {symbol} @ {price}, Qty: {qty}")
    return r


# ==========================
# 🔹 Webhook Endpoint
# ==========================
@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        if request.is_json:
            data = request.get_json()
            msg = data.get("message", "")
        else:
            msg = request.get_data(as_text=True)

        ticker, comment, close_price, interval = msg.split('|')
        symbol = ticker.replace("USDT", "") + "USDT"
        close_price = float(close_price)

        if "LONG" in comment and "EXIT" not in comment:
            r = open_position(symbol, "BUY")
        elif "SHORT" in comment and "EXIT" not in comment:
            r = open_position(symbol, "SELL")
        elif "EXIT_LONG" in comment:
            r = close_position(symbol, "BUY", close_price)
        elif "EXIT_SHORT" in comment:
            r = close_position(symbol, "SELL", close_price)
        else:
            r = {"error": f"Unknown comment: {comment}"}

        return jsonify({"status": "ok", "response": r})

    except Exception as e:
        print("❌ Error in webhook:", e)
        return jsonify({"error": str(e)})


# ==========================
# 🔹 Ping + Keep Alive
# ==========================
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

# ==========================
# 🔹 Run Flask
# ==========================
if __name__ == '__main__':
    port = int(os.getenv("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
