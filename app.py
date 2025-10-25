# app.py
from flask import Flask, request, jsonify
import requests, hmac, hashlib, time, threading, os
from config import *

app = Flask(__name__)

# ===== ENV / SETTINGS =====
EXIT_ORDER_TYPE = os.getenv("EXIT_ORDER_TYPE", "LIMIT").upper()  # LIMIT or MARKET
EXIT_TIMEOUT_SEC = int(os.getenv("EXIT_TIMEOUT_SEC", "15"))      # wait before cancel+market fallback
EXIT_MARKET_DELAY = int(os.getenv("EXIT_MARKET_DELAY", "2"))     # delay before market exit to capture better price
MONITOR_POLL_INTERVAL = 1  # seconds to poll order status while waiting

# Pending exit tracking (symbol -> dict)
pending_exits = {}
pending_lock = threading.Lock()

# ===== Helpers =====
def _sign_query(params):
    query = "&".join([f"{k}={params[k]}" for k in sorted(params.keys())])
    signature = hmac.new(BINANCE_SECRET_KEY.encode(), query.encode(), hashlib.sha256).hexdigest()
    return query + f"&signature={signature}"

def binance_signed_request(http_method, path, params=None):
    """Generic signed request for Binance Futures (USDT-M)."""
    if params is None:
        params = {}
    params["timestamp"] = int(time.time() * 1000)
    query = _sign_query(params)
    url = f"{BASE_URL}{path}?{query}"
    headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
    try:
        if http_method == "GET":
            r = requests.get(url, headers=headers, timeout=10)
        elif http_method == "POST":
            r = requests.post(url, headers=headers, timeout=10)
        elif http_method == "DELETE":
            r = requests.delete(url, headers=headers, timeout=10)
        else:
            raise ValueError("unsupported method")
        return r.json()
    except Exception as e:
        print("❌ Binance request failed:", e)
        return {"error": str(e)}

def set_leverage_and_margin(symbol):
    try:
        binance_signed_request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": LEVERAGE})
        binance_signed_request("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": MARGIN_TYPE})
    except Exception as e:
        print("❌ set_leverage_and_margin error:", e)

# ===== Symbol info & qty helpers =====
SYMBOL_INFO_CACHE = {}

def get_symbol_info(symbol):
    if symbol in SYMBOL_INFO_CACHE:
        return SYMBOL_INFO_CACHE[symbol]
    info = requests.get(f"{BASE_URL}/fapi/v1/exchangeInfo", timeout=10).json()
    for s in info.get("symbols", []):
        if s["symbol"] == symbol:
            SYMBOL_INFO_CACHE[symbol] = s
            return s
    return None

def round_quantity(symbol, qty):
    info = get_symbol_info(symbol)
    if not info:
        return round(qty, 6)
    lot = next((f for f in info["filters"] if f["filterType"] == "LOT_SIZE"), None)
    if not lot:
        return round(qty, 6)
    step_size = float(lot["stepSize"])
    min_qty = float(lot["minQty"])
    # floor to step_size
    steps = int(qty // step_size)
    qty_adj = steps * step_size
    if qty_adj < min_qty:
        qty_adj = min_qty
    # limit precision
    return float(round(qty_adj, 8))

def get_current_price(symbol):
    try:
        res = requests.get(f"{BASE_URL}/fapi/v1/ticker/price", params={"symbol": symbol}, timeout=5).json()
        return float(res.get("price"))
    except Exception as e:
        print("❌ price fetch failed:", e)
        return None

def calculate_quantity(symbol, usdt_value):
    price = get_current_price(symbol)
    if not price or price == 0:
        return 0.0
    qty = usdt_value / price
    return round_quantity(symbol, qty)

# ===== Position & Order helpers =====
def get_position(symbol):
    """Return positionAmt (float) and data (dict) or (0, None) if not found."""
    res = binance_signed_request("GET", "/fapi/v2/positionRisk", {"symbol": symbol})
    if isinstance(res, dict) and res.get("error"):
        return 0.0, None
    if isinstance(res, list) and len(res) > 0:
        posAmt = float(res[0].get("positionAmt", 0))
        return posAmt, res[0]
    return 0.0, None

def get_order_status(symbol, orderId=None, origClientOrderId=None):
    params = {"symbol": symbol}
    if orderId:
        params["orderId"] = orderId
    if origClientOrderId:
        params["origClientOrderId"] = origClientOrderId
    return binance_signed_request("GET", "/fapi/v1/order", params)

def cancel_order(symbol, orderId=None, origClientOrderId=None):
    params = {"symbol": symbol}
    if orderId:
        params["orderId"] = orderId
    if origClientOrderId:
        params["origClientOrderId"] = origClientOrderId
    return binance_signed_request("DELETE", "/fapi/v1/order", params)

# ===== Order Execution =====
def open_position(symbol, side):
    """Market entry. Before opening, ensure pending exit for same symbol is handled."""
    # If an exit is pending for this symbol, cancel it and ensure closure first
    handle_pending_on_new_signal(symbol)

    set_leverage_and_margin(symbol)
    qty = calculate_quantity(symbol, TRADE_AMOUNT)
    if qty <= 0:
        print("❌ calculated qty is zero, aborting entry")
        return {"error": "qty_zero"}

    params = {"symbol": symbol, "side": side, "type": "MARKET", "quantity": qty}
    retries = 3
    resp = {}
    while retries > 0:
        resp = binance_signed_request("POST", "/fapi/v1/order", params)
        if resp and "orderId" in resp:
            break
        print("❌ entry failed, retrying:", resp)
        retries -= 1
        time.sleep(1)
    filled_price = resp.get("avgFillPrice") or (resp.get("fills", [{}])[0].get("price"))
    print(f"📈 ENTRY {side} {symbol} qty={qty} filled_price={filled_price}")
    return resp

def place_limit_exit(symbol, close_side, price, qty):
    """Place a reduce-only limit order to close (reduceOnly=true)."""
    params = {
        "symbol": symbol,
        "side": close_side,
        "type": "LIMIT",
        "price": format(price, 'f'),
        "quantity": qty,
        "timeInForce": "GTC",
        "reduceOnly": "true"  # must be string when signing
    }
    resp = binance_signed_request("POST", "/fapi/v1/order", params)
    return resp

def place_market_exit(symbol, close_side, qty):
    params = {
        "symbol": symbol,
        "side": close_side,
        "type": "MARKET",
        "quantity": qty,
        "reduceOnly": "true"
    }
    resp = binance_signed_request("POST", "/fapi/v1/order", params)
    return resp

# ===== Pending exit monitor =====
def monitor_exit(symbol, orderId, clientOrderId, timeout, close_side):
    """
    Monitor pending limit exit order for `timeout` seconds.
    If not FILLED, cancel and place market exit fallback.
    """
    start = time.time()
    print(f"🔎 Monitoring exit for {symbol} orderId={orderId} timeout={timeout}s")
    filled = False
    while time.time() - start < timeout:
        status = get_order_status(symbol, orderId=orderId)
        if isinstance(status, dict) and status.get("status") == "FILLED":
            filled = True
            print(f"✅ Exit filled for {symbol} orderId={orderId}")
            break
        # if order shows CLOSED/CANCELED or other terminal state, break
        if isinstance(status, dict) and status.get("status") in ("CANCELED", "REJECTED", "EXPIRED"):
            print(f"⚠️ Exit order terminal state {status.get('status')} for {symbol} orderId={orderId}")
            break
        time.sleep(MONITOR_POLL_INTERVAL)

    with pending_lock:
        # if it was filled, remove pending record and return
        if filled:
            pending_exits.pop(symbol, None)
            return

        # otherwise attempt cancel
        print(f"⏳ Exit not filled in time for {symbol}, cancelling orderId={orderId}")
        cancel_result = cancel_order(symbol, orderId=orderId)
        print(f"🗑 Cancel result: {cancel_result}")

        # remove pending regardless
        pending_exits.pop(symbol, None)

    # fallback: place market exit to ensure position is closed
    print(f"⚠️ Placing MARKET fallback exit for {symbol}")
    # determine qty from open position
    posAmt, _ = get_position(symbol)
    if posAmt == 0:
        print(f"⚠️ No open position for {symbol} at fallback time, skipping market fallback")
        return
    qty = round_quantity(symbol, abs(posAmt))
    fallback_resp = place_market_exit(symbol, close_side, qty)
    print(f"⚠️ Fallback market close response: {fallback_resp}")

def handle_pending_on_new_signal(symbol):
    """If a pending exit exists for symbol, cancel it and force market close to ensure consistent state."""
    with pending_lock:
        pending = pending_exits.get(symbol)
        if not pending:
            return
        print(f"🔔 New signal arrived but pending exit exists for {symbol}; cancelling & forcing market close first.")
        # cancel pending order
        try:
            cancel_result = cancel_order(symbol, orderId=pending.get("orderId"))
            print(f"🗑 Cancelled pending exit: {cancel_result}")
        except Exception as e:
            print("❌ failed to cancel pending exit:", e)
        # remove from dict
        pending_exits.pop(symbol, None)

    # after cancel, if there's an open position, close with market to ensure clean state
    posAmt, _ = get_position(symbol)
    if posAmt == 0:
        print(f"ℹ️ No open position to forcibly close for {symbol}")
        return
    # determine which market side to close
    if posAmt > 0:
        close_side = "SELL"
    else:
        close_side = "BUY"
    qty = round_quantity(symbol, abs(posAmt))
    print(f"⏳ Forcing MARKET close before new entry: {symbol} side={close_side} qty={qty}")
    res = place_market_exit(symbol, close_side, qty)
    print(f"🔔 Forced close response: {res}")
    # small wait to ensure exchange processes closure
    time.sleep(1)

# ===== Close position wrapper with safe checks and pending tracking =====
def close_position_safe(symbol, side, price):
    """
    side param indicates the logical side of the open position we want to close:
      - side="BUY"  => close long position (we'll send SELL)
      - side="SELL" => close short position (we'll send BUY)
    """
    posAmt, posData = get_position(symbol)
    if posAmt == 0:
        print(f"⚠️ No open position for {symbol}, skipping exit signal.")
        return {"status": "no_position"}

    # verify that position side matches the requested close
    if side == "BUY" and posAmt <= 0:
        print(f"⚠️ EXIT_LONG requested but position is not long for {symbol} (posAmt={posAmt}) - skipping")
        return {"status": "mismatch_side"}
    if side == "SELL" and posAmt >= 0:
        print(f"⚠️ EXIT_SHORT requested but position is not short for {symbol} (posAmt={posAmt}) - skipping")
        return {"status": "mismatch_side"}

    # determine close_side for orders (sell to close long, buy to close short)
    close_side = "SELL" if side == "BUY" else "BUY"
    qty = round_quantity(symbol, abs(posAmt))
    if qty <= 0:
        print(f"❌ qty computed zero for {symbol}, skipping exit")
        return {"error": "qty_zero"}

    # if EXIT_ORDER_TYPE = MARKET, delay then place market exit
    if EXIT_ORDER_TYPE == "MARKET":
        if EXIT_MARKET_DELAY > 0:
            print(f"⏳ delaying {EXIT_MARKET_DELAY}s before MARKET exit for {symbol}")
            time.sleep(EXIT_MARKET_DELAY)
        print(f"🔔 placing MARKET exit for {symbol} side={close_side} qty={qty}")
        resp = place_market_exit(symbol, close_side, qty)
        print("🔔 market exit resp:", resp)
        return resp

    # else EXIT_ORDER_TYPE == LIMIT: place reduceOnly limit exit and monitor
    resp = place_limit_exit(symbol, close_side, price, qty)
    if not resp or "orderId" not in resp:
        print("❌ failed to place limit exit:", resp)
        return resp

    orderId = resp.get("orderId")
    clientOrderId = resp.get("clientOrderId", None)
    ts = time.time()

    # register pending exit
    with pending_lock:
        pending_exits[symbol] = {
            "orderId": orderId,
            "clientOrderId": clientOrderId,
            "timestamp": ts,
            "side": close_side,
            "price": price
        }
    # start monitoring thread for this pending exit
    monitor_thread = threading.Thread(target=monitor_exit, args=(symbol, orderId, clientOrderId, EXIT_TIMEOUT_SEC, close_side), daemon=True)
    monitor_thread.start()
    print(f"🔔 Limit exit placed for {symbol} orderId={orderId}, monitoring started.")
    return resp

# ===== Webhook endpoint =====
@app.route('/webhook', methods=['POST'])
def webhook():
    raw = request.get_data(as_text=True)
    try:
        ticker, comment, close_price, interval = raw.split('|')
    except Exception as e:
        return jsonify({"error": "invalid message format, expected 4 pipe fields", "raw": raw, "exception": str(e)}), 400

    ticker = ticker.strip()
    comment = comment.strip().upper()
    try:
        close_price = float(close_price)
    except:
        close_price = None

    # normalize symbol (expecting USDT pairs)
    symbol = ticker if ticker.endswith("USDT") else ticker + "USDT"
    print(f"📨 Alert received: {ticker} | {comment} | {close_price} | {interval}")

    # map comment exactly to actions to avoid ambiguity
    if comment == "BUY_ENTRY":
        res = open_position(symbol, "BUY")
        return jsonify({"status": "ok", "action": "open_buy", "response": res})
    elif comment == "SELL_ENTRY":
        res = open_position(symbol, "SELL")
        return jsonify({"status": "ok", "action": "open_sell", "response": res})
    elif comment == "EXIT_LONG":
        # close long positions (side="BUY" means the open position is a BUY/long)
        if close_price is None:
            return jsonify({"error": "missing close price for exit"}), 400
        res = close_position_safe(symbol, "BUY", close_price)
        return jsonify({"status": "ok", "action": "exit_long", "response": res})
    elif comment == "EXIT_SHORT":
        # close short positions (side="SELL" means open short)
        if close_price is None:
            return jsonify({"error": "missing close price for exit"}), 400
        res = close_position_safe(symbol, "SELL", close_price)
        return jsonify({"status": "ok", "action": "exit_short", "response": res})
    else:
        return jsonify({"error": "unknown comment", "comment": comment}), 400

# ===== Ping Endpoint =====
@app.route('/ping', methods=['GET'])
def ping():
    return "pong", 200

# ===== Self-ping (keeps Render service awake) =====
PING_INTERVAL = int(os.getenv("PING_INTERVAL", 300))
SELF_PING_URL = os.getenv("SELF_PING_URL", "").strip()  # set this in env to your https://<render-url>/ping

def self_ping():
    if not SELF_PING_URL:
        print("ℹ️ SELF_PING_URL not configured, self-ping disabled.")
        return
    while True:
        try:
            print("🔄 Self-ping to keep bot alive...")
            requests.get(SELF_PING_URL, timeout=10)
        except Exception as e:
            print("❌ Self-ping failed:", e)
        time.sleep(PING_INTERVAL)

if SELF_PING_URL:
    threading.Thread(target=self_ping, daemon=True).start()

# ===== Start app =====
if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    print(f"⚙️ Starting app (EXIT_ORDER_TYPE={EXIT_ORDER_TYPE}, EXIT_TIMEOUT_SEC={EXIT_TIMEOUT_SEC}, EXIT_MARKET_DELAY={EXIT_MARKET_DELAY})")
    app.run(host="0.0.0.0", port=port)
