# app.py (UPDATED: HT alerts only set trend & message; no trade execution on HT)
from flask import Flask, request, jsonify
import requests, hmac, hashlib, time, threading, os, re
from config import *
from trade_notifier import log_trade_entry, log_trade_exit, trades, send_telegram_message

app = Flask(__name__)

# ----------------------------
# Per-symbol higher-timeframe state
# ----------------------------
# Structure:
# symbol_states = {
#   "BTCUSDT": {
#       "intervals_seen": {30, 240},
#       "ht_interval": 240,                # minutes (higher timeframe)
#       "ht_direction": "BUY"/"SELL"/None, # latest HT direction
#       "last_ht_change": 0                # timestamp of last HT change
#   }
# }
symbol_states = {}
symbol_states_lock = threading.Lock()

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


def get_symbol_info(symbol):
    info = requests.get(f"{BASE_URL}/fapi/v1/exchangeInfo").json()
    for s in info.get("symbols", []):
        if s["symbol"] == symbol:
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
    try:
        positions = binance_signed_request("GET", "/fapi/v2/positionRisk")
        active_positions = [p for p in positions if abs(float(p["positionAmt"])) > 0]
        return len(active_positions)
    except Exception as e:
        print("❌ Failed to fetch active trades:", e)
        return 0


# ===== Calculate Quantity =====
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


# ===== Open Position =====
def open_position(symbol, side, limit_price):
    active_count = count_active_trades()
    if active_count >= MAX_ACTIVE_TRADES:
        print(f"🚫 Max active trades reached ({active_count}/{MAX_ACTIVE_TRADES})")
        return {"status": "max_trades_reached"}

    set_leverage_and_margin(symbol)
    qty = calculate_quantity(symbol)

    # Avoid duplicate entry messages
    if symbol not in trades or trades[symbol].get("closed", True):
        log_trade_entry(symbol, side, "PENDING", limit_price)

    response = binance_signed_request("POST", "/fapi/v1/order", {
        "symbol": symbol,
        "side": side,
        "type": "LIMIT",
        "timeInForce": "GTC",
        "quantity": qty,
        "price": limit_price
    })

    if "orderId" in response:
        order_id = response["orderId"]
        threading.Thread(target=wait_and_notify_filled_entry, args=(symbol, side, order_id), daemon=True).start()

    return response


def wait_and_notify_filled_entry(symbol, side, order_id):
    """Notify as soon as the order is partially or fully filled, only once."""
    notified = False

    while True:
        order_status = binance_signed_request("GET", "/fapi/v1/order", {"symbol": symbol, "orderId": order_id})
        status = order_status.get("status")
        executed_qty = float(order_status.get("executedQty", 0))
        avg_price = float(order_status.get("avgPrice") or order_status.get("price") or 0)

        # Send Telegram entry message as soon as partially filled
        if not notified and status in ("PARTIALLY_FILLED", "FILLED") and executed_qty > 0:
            log_trade_entry(symbol, side, order_id, avg_price)
            notified = True

        # Stop checking once the order is completely filled or canceled
        if status in ("FILLED", "CANCELED", "REJECTED", "EXPIRED"):
            break

        time.sleep(1)


# ===== Market Exit =====
def execute_market_exit(symbol, side):
    pos_data = binance_signed_request("GET", "/fapi/v2/positionRisk", {"symbol": symbol})
    if not pos_data or abs(float(pos_data[0]["positionAmt"])) == 0:
        print(f"⚠️ No active position for {symbol} to close.")
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

    if "orderId" in response:
        threading.Thread(target=wait_and_notify_filled_exit, args=(symbol, response["orderId"]), daemon=True).start()

    return response


def wait_and_notify_filled_exit(symbol, order_id):
    """Wait until exit order fills, clean residuals, and send Telegram notification."""
    while True:
        order_status = binance_signed_request("GET", "/fapi/v1/order", {"symbol": symbol, "orderId": order_id})
        if order_status.get("status") == "FILLED":
            filled_price = float(order_status.get("avgPrice") or order_status.get("price") or 0)
            log_trade_exit(symbol, order_id, filled_price)
            clean_residual_positions(symbol)
            break
        time.sleep(1)


# ===== Auto-clean residual positions =====
def clean_residual_positions(symbol):
    """Closes leftover open orders or 0-amount positions."""
    try:
        binance_signed_request("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol})
        pos_data = binance_signed_request("GET", "/fapi/v2/positionRisk", {"symbol": symbol})
        if pos_data and abs(float(pos_data[0]["positionAmt"])) > 0.00001:
            amt = abs(float(pos_data[0]["positionAmt"]))
            side = "SELL" if float(pos_data[0]["positionAmt"]) > 0 else "BUY"
            binance_signed_request("POST", "/fapi/v1/order", {
                "symbol": symbol,
                "side": side,
                "type": "MARKET",
                "quantity": round_quantity(symbol, amt)
            })
            print(f"🧹 Residual position cleaned for {symbol}")
    except Exception as e:
        print("⚠️ Residual cleanup failed:", e)


# ===== Async Close & Open Logic =====
def async_exit_and_open(symbol, new_side, limit_price):
    def worker():
        pos_data = binance_signed_request("GET", "/fapi/v2/positionRisk", {"symbol": symbol})
        amt = float(pos_data[0]["positionAmt"]) if pos_data else 0
        opposite_side = None

        if amt > 0 and new_side == "SELL":
            opposite_side = "BUY"
        elif amt < 0 and new_side == "BUY":
            opposite_side = "SELL"

        if opposite_side:
            execute_market_exit(symbol, opposite_side)
            time.sleep(OPPOSITE_CLOSE_DELAY)

        open_position(symbol, new_side, limit_price)

    threading.Thread(target=worker, daemon=True).start()


# ===== Interval parsing helpers =====
def interval_to_minutes(interval_str: str) -> int:
    """
    Convert TradingView interval string to minutes.
    Handles examples: "30", "240", "4h", "1h", "60", "D", "1D", "W", "M"
    Returns integer minutes (D -> 1440, W -> 10080, M -> 43200)
    If unknown, returns a large number (so it won't be treated as LT accidentally).
    """
    s = str(interval_str).strip()
    # direct numeric (minutes)
    if re.fullmatch(r"\d+", s):
        return int(s)
    # hours like "4h" or "1H"
    m = re.fullmatch(r"(\d+)\s*[hH]", s)
    if m:
        return int(m.group(1)) * 60
    # days like "D" or "1D"
    m = re.fullmatch(r"(\d+)\s*[dD]", s)
    if m:
        return int(m.group(1)) * 24 * 60
    if s.upper() == "D" or s.upper() == "1D":
        return 24 * 60
    if s.upper() == "W":
        return 7 * 24 * 60
    if s.upper() == "M":
        return 30 * 24 * 60
    # fallback: try to extract digits
    digits = re.findall(r"\d+", s)
    if digits:
        return int(digits[0])
    # unknown -> return very large so it's treated as HT
    return 10**6


def update_symbol_seen_interval(symbol: str, minutes: int):
    with symbol_states_lock:
        st = symbol_states.get(symbol)
        if not st:
            symbol_states[symbol] = {
                "intervals_seen": set([minutes]),
                "ht_interval": None,
                "ht_direction": None,
                "last_ht_change": 0
            }
            return
        st["intervals_seen"].add(minutes)
        # if we have seen at least two intervals, determine HT interval (max minutes)
        if len(st["intervals_seen"]) >= 2:
            st["ht_interval"] = max(st["intervals_seen"])


def set_ht_direction(symbol: str, direction: str):
    """Set HT direction and notify if changed."""
    direction = direction.upper()
    now = time.time()
    with symbol_states_lock:
        st = symbol_states.get(symbol)
        if not st:
            # initialize HT tracking if missing
            symbol_states[symbol] = {
                "intervals_seen": set(),
                "ht_interval": None,
                "ht_direction": direction,
                "last_ht_change": now
            }
            # notify initial HT direction using exact requested format
            send_telegram_message(f"🔔 Higher timeframe trend set for #{symbol}: {direction}")
            return

        prev = st.get("ht_direction")
        if prev != direction:
            st["ht_direction"] = direction
            st["last_ht_change"] = now
            # send Telegram message about HT trend change
            if prev:
                send_telegram_message(f"🔁 Higher timeframe trend changed for #{symbol}: {prev} -> {direction}")
            else:
                send_telegram_message(f"🔔 Higher timeframe trend set for #{symbol}: {direction}")


def get_ht_info(symbol: str):
    with symbol_states_lock:
        st = symbol_states.get(symbol)
        if not st:
            return None
        return {
            "intervals_seen": set(st["intervals_seen"]),
            "ht_interval": st["ht_interval"],
            "ht_direction": st["ht_direction"]
        }


# ===== Webhook =====
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_data(as_text=True)
    try:
        parts = [p.strip() for p in data.split("|")]
        if len(parts) >= 6:
            ticker, comment, close_price, bar_high, bar_low, interval = parts[:6]
        else:
            ticker, comment, close_price, interval = parts[0], parts[1], parts[2], parts[-1]
            bar_high = bar_low = None

        # Normalize symbol
        symbol = ticker.replace("USDT", "") + "USDT"
        close_price = float(close_price)
        interval_minutes = interval_to_minutes(interval)

        # Update seen intervals for this symbol (used to decide HT)
        update_symbol_seen_interval(symbol, interval_minutes)
        ht_info = get_ht_info(symbol)
        ht_interval = ht_info["ht_interval"] if ht_info else None
        ht_direction = ht_info["ht_direction"] if ht_info else None

        # Determine if this incoming alert is HT or LT
        is_ht_alert = False
        is_lt_alert = False
        if ht_interval is None:
            # Not decided yet: if we have only one interval seen, we treat this alert as 'candidate'
            # If interval is relatively large (>= 180 minutes) treat as HT candidate
            is_ht_alert = interval_minutes >= 180
            is_lt_alert = not is_ht_alert
        else:
            if interval_minutes >= ht_interval:
                is_ht_alert = True
            else:
                is_lt_alert = True

        # Normalize comment to uppercase
        comment_u = (comment or "").upper().strip()

        # Helper boolean checks
        is_entry = comment_u in ("BUY_ENTRY", "SELL_ENTRY")
        is_cross_exit_short = comment_u == "CROSS_EXIT_SHORT"
        is_cross_exit_long = comment_u == "CROSS_EXIT_LONG"
        is_exit_long = comment_u == "EXIT_LONG"
        is_exit_short = comment_u == "EXIT_SHORT"
        is_exit = is_cross_exit_short or is_cross_exit_long or is_exit_long or is_exit_short

        # ----- HIGHER-TIMEFRAME ALERT -----
        if is_ht_alert:
            # HT entries only set the trend and send the message — DO NOT execute trades
            if comment_u == "BUY_ENTRY":
                set_ht_direction(symbol, "BUY")
                # message already sent from set_ht_direction
                return jsonify({"status": "ok", "message": "HT BUY trend set"}), 200
            elif comment_u == "SELL_ENTRY":
                set_ht_direction(symbol, "SELL")
                return jsonify({"status": "ok", "message": "HT SELL trend set"}), 200
            else:
                # For HT exits or other alerts: ignore (do not execute)
                return jsonify({"status": "ignored", "message": "HT ignored (only trend-setting allowed)"}), 200

        # ----- LOWER-TIMEFRAME ALERT -----
        if is_lt_alert:
            # If it's an entry, allow only when HT direction is known AND matches
            if is_entry:
                if ht_direction is None:
                    # HT direction not known yet -> ignore (safe default) and notify
                    send_telegram_message(f"⛔ LT entry ignored for #{symbol} at {interval} — HT direction unknown. LT signal: {comment_u}")
                    return jsonify({"status": "ignored", "reason": "ht_unknown"}), 200

                entry_dir = "BUY" if comment_u == "BUY_ENTRY" else "SELL"

                # --- Case 1: matches HT direction → normal open
                if entry_dir == ht_direction:
                    async_exit_and_open(symbol, entry_dir, close_price)
                    return jsonify({"status": "ok", "message": "LT entry processed (matches HT)"}), 200

                # --- Case 2: opposite to HT → check if existing position needs to be closed
                pos_data = binance_signed_request("GET", "/fapi/v2/positionRisk", {"symbol": symbol})
                if pos_data and abs(float(pos_data[0]["positionAmt"])) > 0:
                    amt = float(pos_data[0]["positionAmt"])
                    side_open = "BUY" if amt > 0 else "SELL"

                    # If open position is opposite of LT signal, close it
                    if side_open != entry_dir:
                        execute_market_exit(symbol, side_open)
                        send_telegram_message(f"✅ LT opposite signal used to close existing #{symbol} {side_open} position at {close_price} (HT={ht_direction})")
                        return jsonify({"status": "ok", "message": "LT opposite signal closed existing position"}), 200

                # --- Case 3: no open position → block counter-trend entry
                send_telegram_message(f"⛔ LT entry blocked for #{symbol} at {interval} — HT is {ht_direction}, LT signalled {entry_dir}, and no open position.")
                return jsonify({"status": "blocked_by_ht", "ht_direction": ht_direction}), 200

            # If it's an exit (any exit), always allow — close existing positions
            if is_exit:
                # Map exit comments to sides for execute_market_exit
                if is_cross_exit_short or is_exit_long:
                    execute_market_exit(symbol, "BUY")
                elif is_cross_exit_long or is_exit_short:
                    execute_market_exit(symbol, "SELL")
                return jsonify({"status": "ok", "message": "LT exit processed"}), 200


# ===== Ping =====
@app.route("/ping", methods=["GET"])
def ping():
    return "pong", 200


# ===== Self Ping =====
def self_ping():
    while True:
        try:
            requests.get(f"https://tradingview-binance-2o1v.onrender.com/ping")
        except:
            pass
        time.sleep(5 * 60)


threading.Thread(target=self_ping, daemon=True).start()


# ===== Run Flask =====
if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
