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
    # Determine which side to close
    close_side = "SELL" if side == "BUY" else "BUY"
    
    # Check if there is an open position
    pos_data = binance_signed_request("GET", "/fapi/v2/positionRisk", {"symbol": symbol})
    if not pos_data or float(pos_data[0]["positionAmt"]) == 0:
        print(f"⚠️ No open position for {symbol}, skipping exit.")
        return {"status": "no_position"}
    
    # Only exit if position exists
    qty = abs(float(pos_data[0]["positionAmt"]))  # Use actual open qty
    qty = round_quantity(symbol, qty)
    
    retries = 3
    while retries > 0:
        response = binance_signed_request("POST", "/fapi/v1/order", {
            "symbol": symbol,
            "side": close_side,
            "type": "LIMIT",
            "price": price,
            "quantity": qty,
            "timeInForce": "GTC"
        })
        if "orderId" in response:
            break
        else:
            print("❌ Exit failed, retrying...", response)
            retries -= 1
            time.sleep(1)
    print(f"✖️ {close_side} EXIT: {symbol} @ {price}, Qty: {qty}")
    return response

# ===== Webhook Endpoint =====
@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_data(as_text=True)
    try:
        ticker, comment, close_price, interval = data.split('|')
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
            r = {"error": "Unknown comment"}

        return jsonify({"status": "ok", "response": r})

    except Exception as e:
        return jsonify({"error": str(e)})

# ===== Ping Endpoint =====
@app.route('/ping', methods=['GET'])
def ping():
    return "pong", 200

# ===== Self-Ping Thread =====
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

# ===== Run Flask =====
if __name__ == '__main__':
    port = int(os.getenv("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
