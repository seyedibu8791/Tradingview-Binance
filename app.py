from flask import Flask, request, jsonify
import requests, hmac, hashlib, time, threading, os

from config import *

app = Flask(__name__)

# ===== ENV SETTINGS =====
EXIT_ORDER_TYPE = os.getenv("EXIT_ORDER_TYPE", "LIMIT").upper()  # LIMIT or MARKET
EXIT_TIMEOUT_SEC = int(os.getenv("EXIT_TIMEOUT_SEC", "10"))      # Timeout for limit exit

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
OPEN_EXIT_ORDERS = {}  # Track pending exit orders per symbol

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

def calculate_quantity(symbol, usdt_value):
    try:
        price_data = requests.get(f"{BASE_URL}/fapi/v1/ticker/price", params={"symbol": symbol}).json()
        price = float(price_data["price"])
        qty = usdt_value / price
        qty = round_quantity(symbol, qty)
        return qty
    except:
        return 0.001

# ===== Order Execution =====
def open_position(symbol, side):
    # Cancel pending exit if new entry arrives
    if symbol in OPEN_EXIT_ORDERS:
        cancel_exit_order(symbol)
        # Optionally close existing position before new entry
        pos_data = binance_signed_request("GET", "/fapi/v2/positionRisk", {"symbol": symbol})
        if pos_data and float(pos_data[0]["positionAmt"]) != 0:
            close_side = "SELL" if float(pos_data[0]["positionAmt"]) > 0 else "BUY"
            close_position(symbol, "BUY" if close_side=="SELL" else "SELL", 0)  # market close

    set_leverage_and_margin(symbol)
    qty = calculate_quantity(symbol, TRADE_AMOUNT)
    retries = 3
    while retries > 0:
        response = binance_signed_request("POST", "/fapi/v1/order", {
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            "quantity": qty
        })
        if "orderId" in response:
            break
        else:
            print("❌ Entry failed, retrying...", response)
            retries -= 1
            time.sleep(1)
    filled_price = response.get("avgFillPrice") or (response.get("fills", [{}])[0].get("price"))
    print(f"📊 {side} ENTRY: {symbol}, Qty: {qty}, Filled Price: {filled_price}")
    return response

def close_position(symbol, side, price):
    close_side = "SELL" if side == "BUY" else "BUY"
    pos_data = binance_signed_request("GET", "/fapi/v2/positionRisk", {"symbol": symbol})
    if not pos_data or float(pos_data[0]["positionAmt"]) == 0:
        print(f"⚠️ No open position for {symbol}, skipping exit.")
        return {"status": "no_position"}

    qty = abs(float(pos_data[0]["positionAmt"]))
    qty = round_quantity(symbol, qty)

    if EXIT_ORDER_TYPE == "MARKET":
        print(f"⏳ Waiting {EXIT_MARKET_DELAY}s before MARKET exit to capture price...")
        time.sleep(EXIT_MARKET_DELAY)
        response = binance_signed_request("POST", "/fapi/v1/order", {
            "symbol": symbol,
            "side": close_side,
            "type": "MARKET",
            "quantity": qty
        })
        print(f"✖️ {close_side} MARKET EXIT: {symbol}, Qty: {qty}")
    else:
        response = binance_signed_request("POST", "/fapi/v1/order", {
            "symbol": symbol,
            "side": close_side,
            "type": "LIMIT",
            "price": price,
            "quantity": qty,
            "timeInForce": "GTC"
        })
        print(f"✖️ {close_side} LIMIT EXIT: {symbol} @ {price}, Qty: {qty}")
        # Track pending exit order
        OPEN_EXIT_ORDERS[symbol] = response
        # Start thread to monitor timeout
        threading.Thread(target=monitor_exit_order, args=(symbol, response.get("orderId"), close_side, qty), daemon=True).start()

    return response

def cancel_exit_order(symbol):
    if symbol in OPEN_EXIT_ORDERS:
        order_id = OPEN_EXIT_ORDERS[symbol].get("orderId")
        if order_id:
            binance_signed_request("DELETE", "/fapi/v1/order", {"symbol": symbol, "orderId": order_id})
            print(f"⚠️ Pending exit order for {symbol} canceled")
        OPEN_EXIT_ORDERS.pop(symbol, None)

def monitor_exit_order(symbol, order_id, side, qty):
    start_time = time.time()
    while time.time() - start_time < EXIT_TIMEOUT_SEC:
        try:
            status = binance_signed_request("GET", "/fapi/v1/order", {"symbol": symbol, "orderId": order_id})
            if status.get("status") == "FILLED":
                print(f"✅ LIMIT exit filled for {symbol}")
                OPEN_EXIT_ORDERS.pop(symbol, None)
                return
        except:
            pass
        time.sleep(1)
    # Timeout reached → cancel & market exit
    print(f"⏰ LIMIT exit timeout reached for {symbol}, executing MARKET exit")
    cancel_exit_order(symbol)
    binance_signed_request("POST", "/fapi/v1/order", {
        "symbol": symbol,
        "side": side,
        "type": "MARKET",
        "quantity": qty
    })
    OPEN_EXIT_ORDERS.pop(symbol, None)

# ===== Webhook Endpoint =====
@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_data(as_text=True)
    try:
        ticker, comment, close_price, interval = data.split('|')
        symbol = ticker.replace("USDT", "") + "USDT"
        close_price = float(close_price)

        if comment == "BUY_ENTRY":
            r = open_position(symbol, "BUY")
        elif comment == "SELL_ENTRY":
            r = open_position(symbol, "SELL")
        elif comment == "EXIT_LONG":
            r = close_position(symbol, "BUY", close_price)
        elif comment == "EXIT_SHORT":
            r = close_position(symbol, "SELL", close_price)
        else:
            r = {"error": "Unknown comment"}

        return jsonify({"status": "ok", "response": r})

    except Exception as e:
        return jsonify({"error": str(e)})

# ===== Ping Endpoint =====
@app.route('/ping', methods=['GET'])
def ping():
    return "pong", 200

# ===== Self-Ping Thread =====
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
