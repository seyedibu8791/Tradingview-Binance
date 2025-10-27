from flask import Flask, request, jsonify
import requests, hmac, hashlib, time, threading, os
from config import *

app = Flask(__name__)

# ===== Env / Exit Limit Settings =====
# Use bar high/low for exit limit price when True
USE_BAR_EXIT = os.getenv("USE_BAR_HIGH_LOW_EXIT", "True").lower() in ("1", "true", "yes")
# How many seconds to wait for the LIMIT exit to fill before cancelling and doing MARKET exit
EXIT_WAIT_LIMIT_SECS = int(os.getenv("EXIT_WAIT_LIMIT_SECS", "60"))

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
OPEN_LIMIT_ORDERS = {}       # Pending LIMIT entry orders (symbol -> orderId)
EXIT_LIMIT_ORDERS = {}       # Pending LIMIT exit orders (symbol -> orderId)
EXIT_MONITORS = {}           # Monitor threads (symbol -> thread)
EXIT_LOCK = threading.Lock() # Lock to handle simultaneous exit/entry

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
    # align qty to step_size (avoid floating remainder issues)
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

def cancel_limit_entry(symbol):
    order_id = OPEN_LIMIT_ORDERS.get(symbol)
    if order_id:
        try:
            binance_signed_request("DELETE", "/fapi/v1/order", {"symbol": symbol, "orderId": order_id})
            print(f"⚠️ Pending LIMIT entry for {symbol} canceled")
        except Exception as e:
            print(f"❌ Failed to cancel pending LIMIT for {symbol}: {e}")
        OPEN_LIMIT_ORDERS.pop(symbol, None)

def cancel_exit_limit(symbol):
    order_id = EXIT_LIMIT_ORDERS.get(symbol)
    if order_id:
        try:
            binance_signed_request("DELETE", "/fapi/v1/order", {"symbol": symbol, "orderId": order_id})
            print(f"⚠️ Pending LIMIT exit for {symbol} canceled")
        except Exception as e:
            print(f"❌ Failed to cancel pending exit LIMIT for {symbol}: {e}")
        EXIT_LIMIT_ORDERS.pop(symbol, None)

def check_partial_fill(symbol):
    order_id = OPEN_LIMIT_ORDERS.get(symbol)
    if not order_id:
        return False
    order_info = binance_signed_request("GET", "/fapi/v1/order", {"symbol": symbol, "orderId": order_id})
    if order_info.get("status") in ["FILLED", "PARTIALLY_FILLED"]:
        print(f"✅ {symbol} order is partially/fully filled. Trade considered active.")
        return True
    return False

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

# ===== Exit LIMIT monitor =====
def monitor_exit_limit(symbol, order_id, side, qty):
    """Wait up to EXIT_WAIT_LIMIT_SECS for the LIMIT exit to fill. If not filled -> cancel & MARKET exit."""
    start = time.time()
    while time.time() - start < EXIT_WAIT_LIMIT_SECS:
        try:
            status = binance_signed_request("GET", "/fapi/v1/order", {"symbol": symbol, "orderId": order_id})
            if status.get("status") == "FILLED":
                print(f"✅ LIMIT exit filled for {symbol} orderId={order_id}")
                # Clear tracking
                with EXIT_LOCK:
                    EXIT_LIMIT_ORDERS.pop(symbol, None)
                    EXIT_MONITORS.pop(symbol, None)
                return
            # if PARTIALLY_FILLED we still consider position partially closed; monitor until fully filled or timeout
        except Exception as e:
            print("❌ monitor_exit_limit error:", e)
        time.sleep(1)

    # Timeout reached -> cancel the limit and market exit
    print(f"⏰ LIMIT exit timeout for {symbol}, cancelling LIMIT and executing MARKET exit")
    try:
        binance_signed_request("DELETE", "/fapi/v1/order", {"symbol": symbol, "orderId": order_id})
    except Exception as e:
        print("❌ Failed to cancel unfilled LIMIT exit:", e)
    with EXIT_LOCK:
        EXIT_LIMIT_ORDERS.pop(symbol, None)
        EXIT_MONITORS.pop(symbol, None)

    # Execute market exit to ensure position closed
    execute_market_exit(symbol, side)

def close_position(symbol, side, price, bar_high=None, bar_low=None):
    """
    Attempt exit as LIMIT first (use bar_high for long, bar_low for short if USE_BAR_EXIT=True).
    If LIMIT not filled within EXIT_WAIT_LIMIT_SECS -> cancel & market exit.
    """
    # Cancel any pending entry order
    cancel_limit_entry(symbol)

    # If a previous exit limit monitor exists, cancel it (we will replace)
    with EXIT_LOCK:
        existing_monitor = EXIT_MONITORS.get(symbol)
        if existing_monitor and existing_monitor.is_alive():
            print(f"⚠️ Cancelling existing exit monitor for {symbol} to start a new one.")
            # cancel existing exit LIMIT order too
            cancel_exit_limit(symbol)
            # allow the old thread to finish on its own (it will find its order missing or be popped)

    # If there's no open position, skip
    pos_data = binance_signed_request("GET", "/fapi/v2/positionRisk", {"symbol": symbol})
    if not pos_data or abs(float(pos_data[0]["positionAmt"])) == 0:
        print(f"⚠️ No open position for {symbol}, skipping exit.")
        return {"status": "no_position"}

    qty = abs(float(pos_data[0]["positionAmt"]))
    qty = round_quantity(symbol, qty)
    close_side = "SELL" if side == "BUY" else "BUY"

    # Choose limit exit price
    limit_price = None
    if USE_BAR_EXIT and bar_high is not None and bar_low is not None:
        if side == "BUY":
            # exit long -> try to sell at bar's high
            limit_price = float(bar_high)
        else:
            # exit short -> try to buy at bar's low
            limit_price = float(bar_low)
    else:
        # fallback to provided price param (strategy.order.price or close)
        limit_price = price

    # Place LIMIT exit order with reduceOnly=true
    try:
        exit_resp = binance_signed_request("POST", "/fapi/v1/order", {
            "symbol": symbol,
            "side": close_side,
            "type": "LIMIT",
            "timeInForce": "GTC",
            "quantity": qty,
            "price": limit_price,
            "reduceOnly": "true"
        })
    except Exception as e:
        print("❌ Failed to place LIMIT exit order:", e)
        exit_resp = {"error": str(e)}

    if not isinstance(exit_resp, dict) or "orderId" not in exit_resp:
        print(f"❌ LIMIT exit order failed for {symbol}:", exit_resp)
        # fallback to immediate market exit
        return execute_market_exit(symbol, side)

    order_id = exit_resp["orderId"]
    with EXIT_LOCK:
        EXIT_LIMIT_ORDERS[symbol] = order_id

    # Start monitor thread
    t = threading.Thread(target=monitor_exit_limit, args=(symbol, order_id, side, qty), daemon=True)
    with EXIT_LOCK:
        EXIT_MONITORS[symbol] = t
    t.start()
    print(f"📨 LIMIT exit placed for {symbol} @ {limit_price} (orderId={order_id}) — waiting {EXIT_WAIT_LIMIT_SECS}s for fill")
    return exit_resp

def open_position(symbol, side, limit_price):
    """Place a LIMIT entry using TradingView alert price"""
    active_count = count_active_trades()
    if active_count >= MAX_ACTIVE_TRADES:
        print(f"🚫 Trade limit reached ({active_count}/{MAX_ACTIVE_TRADES}). Skipping {symbol} {side}.")
        return {"status": "max_trades_reached", "active_trades": active_count}

    # If an exit limit is pending for this symbol, cancel it and execute immediate market exit
    with EXIT_LOCK:
        pending_exit_id = EXIT_LIMIT_ORDERS.get(symbol)
        pending_monitor = EXIT_MONITORS.get(symbol)
        if pending_exit_id:
            print(f"⚠️ New entry for {symbol} detected while exit pending — cancelling exit LIMIT and executing MARKET exit first.")
            cancel_exit_limit(symbol)
            # if monitor thread exists, we leave it; monitor will find order missing and exit.
            # execute immediate market exit to ensure closure
            execute_market_exit(symbol, side="BUY" if side=="SELL" else "SELL")
            # clear monitor if present
            if pending_monitor and pending_monitor.is_alive():
                EXIT_MONITORS.pop(symbol, None)
            EXIT_LIMIT_ORDERS.pop(symbol, None)

    # Close existing position if any (ensures single position per symbol)
    pos_data = binance_signed_request("GET", "/fapi/v2/positionRisk", {"symbol": symbol})
    if pos_data and float(pos_data[0]["positionAmt"]) != 0:
        close_side = "SELL" if float(pos_data[0]["positionAmt"]) > 0 else "BUY"
        close_position(symbol, close_side, 0)

    set_leverage_and_margin(symbol)
    qty = calculate_quantity(symbol)

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
        if isinstance(response, dict) and "orderId" in response:
            OPEN_LIMIT_ORDERS[symbol] = response["orderId"]
            break
        else:
            print("❌ Limit Entry failed, retrying...", response)
            retries -= 1
            time.sleep(1)

    print(f"📊 {side} LIMIT ENTRY: {symbol}, Qty: {qty}, Price: {limit_price}")
    return response

# ===== Webhook =====
@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_data(as_text=True)
    try:
        # Backwards-compatible parsing:
        # supported forms:
        # 1) ticker|comment|close|interval
        # 2) ticker|comment|close|high|low|interval
        parts = data.split('|')
        # normalize whitespace
        parts = [p.strip() for p in parts]

        if len(parts) == 4:
            ticker, comment, close_price, interval = parts
            bar_high = bar_low = None
        elif len(parts) >= 6:
            ticker, comment, close_price, bar_high, bar_low, interval = parts[:6]
        else:
            # fallback: attempt 4-split and ignore extra
            ticker, comment, close_price, interval = parts[0], parts[1], parts[2], parts[-1]
            bar_high = bar_low = None

        symbol = ticker.replace("USDT", "") + "USDT"
        close_price = float(close_price)
        bar_high = float(bar_high) if (bar_high is not None and bar_high != "") else None
        bar_low = float(bar_low) if (bar_low is not None and bar_low != "") else None

        if comment == "BUY_ENTRY":
            r = open_position(symbol, "BUY", close_price)
        elif comment == "SELL_ENTRY":
            r = open_position(symbol, "SELL", close_price)
        elif comment == "EXIT_LONG":
            # For long exit: use bar_high as limit (if provided and USE_BAR_EXIT enabled)
            r = close_position(symbol, "BUY", close_price, bar_high=bar_high, bar_low=bar_low)
        elif comment == "EXIT_SHORT":
            # For short exit: use bar_low as limit
            r = close_position(symbol, "SELL", close_price, bar_high=bar_high, bar_low=bar_low)
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
