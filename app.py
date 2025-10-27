from flask import Flask, request, jsonify
import requests, hmac, hashlib, time, threading, os
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
OPEN_LIMIT_ORDERS = {}  # Track pending LIMIT entries per symbol

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
    """Return number of currently open positions"""
    try:
        positions = binance_signed_request("GET", "/fapi/v2/positionRisk")
        active_positions = [p for p in positions if abs(float(p["positionAmt"])) > 0]
        return len(active_positions)
    except Exception as e:
        print("❌ Failed to fetch active trades:", e)
        return 0

# ===== Order Execution =====
def calculate_quantity(symbol):
    """Calculate quantity using TRADE_AMOUNT x LEVERAGE for total position size"""
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

def cancel_limit_entry(symbol):
    """Cancel pending LIMIT entry if exists"""
    order_id = OPEN_LIMIT_ORDERS.get(symbol)
    if order_id:
        try:
            binance_signed_request("DELETE", "/fapi/v1/order", {"symbol": symbol, "orderId": order_id})
            print(f"⚠️ Pending LIMIT entry for {symbol} canceled")
        except Exception as e:
            print(f"❌ Failed to cancel pending LIMIT for {symbol}: {e}")
        OPEN_LIMIT_ORDERS.pop(symbol, None)

def check_partial_fill(symbol):
    """Check if LIMIT order is partially filled; if so, treat as open position"""
    order_id = OPEN_LIMIT_ORDERS.get(symbol)
    if not order_id:
        return False

    order_info = binance_signed_request("GET", "/fapi/v1/order", {"symbol": symbol, "orderId": order_id})
    if order_info.get("status") in ["FILLED", "PARTIALLY_FILLED"]:
        print(f"✅ {symbol} order is partially/fully filled. Trade considered active.")
        return True
    return False

def open_position(symbol, side, limit_price):
    """Place a LIMIT order using TradingView alert price"""
    
    # ✅ Check max active trades
    active_count = count_active_trades()
    if active_count >= MAX_ACTIVE_TRADES:
        print(f"🚫 Trade limit reached ({active_count}/{MAX_ACTIVE_TRADES}). Skipping {symbol} {side}.")
        return {"status": "max_trades_reached", "active_trades": active_count}

    # Close any existing position if needed
    pos_data = binance_signed_request("GET", "/fapi/v2/positionRisk", {"symbol": symbol})
    if pos_data and float(pos_data[0]["positionAmt"]) != 0:
        close_side = "SELL" if float(pos_data[0]["positionAmt"]) > 0 else "BUY"
        print(f"⚠️ Existing position detected for {symbol}, closing first...")
        close_position(symbol, close_side, 0)

    set_leverage_and_margin(symbol)
    qty = calculate_quantity(symbol)

    # Place LIMIT order
    retries = 3
    response = None
    while retries > 0:
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
            break
        else:
            print("❌ Limit Entry failed, retrying...", response)
            retries -= 1
            time.sleep(1)

    print(f"📊 {side} LIMIT ENTRY: {symbol}, Qty: {qty}, Price: {limit_price}")
    return response

def close_position(symbol, side, price):
    """Cancel pending LIMIT and close any open (partial/full) position"""
    # Cancel any pending limit entry first
    cancel_limit_entry(symbol)

    # Check if trade was partially filled (treat as open)
    check_partial_fill(symbol)

    close_side = "SELL" if side == "BUY" else "BUY"
    pos_data = binance_signed_request("GET", "/fapi/v2/positionRisk", {"symbol": symbol})

    if not pos_data or abs(float(pos_data[0]["positionAmt"])) == 0:
        print(f"⚠️ No open position for {symbol}, skipping exit.")
        return {"status": "no_position"}

    qty = abs(float(pos_data[0]["positionAmt"]))
    qty = round_quantity(symbol, qty)

    print(f"⏳ Waiting {EXIT_MARKET_DELAY}s before MARKET exit to capture better price...")
    time.sleep(EXIT_MARKET_DELAY)

    response = binance_signed_request("POST", "/fapi/v1/order", {
        "symbol": symbol,
        "side": close_side,
        "type": "MARKET",
        "quantity": qty
    })

    print(f"✖️ {close_side} MARKET EXIT: {symbol}, Qty: {qty}")
    return response

# ===== Webhook =====
@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_data(as_text=True)
    try:
        ticker, comment, close_price, interval = data.split('|')
        symbol = ticker.replace("USDT", "") + "USDT"
        close_price = float(close_price)

        if comment == "BUY_ENTRY":
            r = open_position(symbol, "BUY", close_price)
        elif comment == "SELL_ENTRY":
            r = open_position(symbol, "SELL", close_price)
        elif comment == "EXIT_LONG":
            r = close_position(symbol, "BUY", close_price)
        elif comment == "EXIT_SHORT":
            r = close_position(symbol, "SELL", close_price)
        else:
            r = {"error": "Unknown comment"}

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
