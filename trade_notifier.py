# trade_notifier.py

import requests, threading, time, datetime, os
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TRADE_AMOUNT, LEVERAGE

trades = {}  # Stores active and closed trades

# ==============================
# 🔹 Utility Functions
# ==============================

def normalize_symbol(symbol: str) -> str:
    """Normalize symbol for consistent matching (e.g. BTC/USDT -> BTCUSDT)."""
    return symbol.strip().replace("/", "").replace("_", "").upper()

# ==============================
# 🔹 Telegram Messaging
# ==============================

def send_telegram_message(message: str):
    """Send formatted message to Telegram."""
    try:
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            print("⚠️ Telegram credentials missing, message not sent.")
            return

        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }
        response = requests.post(url, data=payload)
        if response.status_code != 200:
            print(f"❌ Telegram send failed: {response.text}")
    except Exception as e:
        print("❌ Telegram send error:", e)

# ==============================
# 🔹 Trade Entry Logging
# ==============================

def log_trade_entry(symbol, side, order_id, filled_price):
    """Log new trade entry and send Telegram notification."""
    symbol = normalize_symbol(symbol)
    print(f"📩 [ENTRY] {symbol} | {side} | {filled_price}")  # Debug

    trades[symbol] = {
        "side": side,
        "entry_price": filled_price,
        "order_id": order_id,
        "closed": False,
        "exit_price": None,
        "pnl": 0,
        "pnl_percent": 0
    }

    message = f"""📈 <b>ENTRY SIGNAL</b>
Symbol: <b>#{symbol}</b>
Side: {side} 💹
Exchange: Binance Futures
Leverage: {LEVERAGE}x
------------------------
💰 Entry Price: {filled_price}
💵 Trade Amount: ${TRADE_AMOUNT}
------------------------
⚠️ Wait for Exit Signal!"""
    send_telegram_message(message)

# ==============================
# 🔹 Trade Exit Logging
# ==============================

def log_trade_exit(symbol, order_id, filled_price):
    """Log trade exit, calculate PnL, and send Telegram notification."""
    symbol = normalize_symbol(symbol)
    print(f"📤 [EXIT] Signal received for {symbol} at {filled_price}")  # Debug

    if symbol not in trades:
        print(f"⚠️ No open trade found for {symbol}. Existing: {list(trades.keys())}")
        send_telegram_message(f"⚠️ Exit signal received for #{symbol}, but no open trade found.")
        return

    trade = trades[symbol]
    if trade["closed"]:
        print(f"⚠️ Trade for {symbol} already closed.")
        return

    trade["exit_price"] = filled_price
    trade["closed"] = True

    # Calculate PnL using leveraged notional
    entry = trade["entry_price"]
    exit = trade["exit_price"]
    qty = TRADE_AMOUNT  # notional USD per trade

    if trade["side"].upper() == "BUY":
        pnl = (exit - entry) * qty * LEVERAGE / entry
        pnl_percent = ((exit - entry) / entry) * 100 * LEVERAGE
    else:
        pnl = (entry - exit) * qty * LEVERAGE / entry
        pnl_percent = ((entry - exit) / entry) * 100 * LEVERAGE

    trade["pnl"] = round(pnl, 2)
    trade["pnl_percent"] = round(pnl_percent, 2)

    status_icon = "✅" if pnl > 0 else "⛔️"
    message = f"""📉 <b>EXIT SIGNAL</b> {status_icon}
Symbol: <b>#{symbol}</b>
Side: {trade['side']}
------------------------
Entry Price: {trade['entry_price']}
Exit Price: {trade['exit_price']}
------------------------
PnL: {trade['pnl_percent']}%
Profit/Loss: ${trade['pnl']}"""
    send_telegram_message(message)

# ==============================
# 🔹 Daily Summary Report
# ==============================

def send_daily_summary():
    """Sends a trade summary every day at 00:00 IST."""
    while True:
        now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5.5)))  # IST
        next_run = now.replace(hour=0, minute=0, second=0, microsecond=0) + datetime.timedelta(days=1)
        sleep_seconds = (next_run - now).total_seconds()
        time.sleep(sleep_seconds)

        total_signals = len(trades)
        profitable = sum(1 for t in trades.values() if t["closed"] and t["pnl"] > 0)
        lost = sum(1 for t in trades.values() if t["closed"] and t["pnl"] < 0)
        cancelled = sum(1 for t in trades.values() if not t["closed"])
        net_pnl_percent = round(sum(t["pnl_percent"] for t in trades.values() if t["closed"]), 2)

        detailed_msg = ""
        for t_sym, t in trades.items():
            if t["closed"]:
                status_icon = "✅" if t["pnl"] > 0 else "⛔️"
                detailed_msg += f"#{t_sym} {t['side']} {status_icon} | Entry: {t['entry_price']} | Exit: {t['exit_price']} | PnL%: {t['pnl_percent']} | PnL$: {t['pnl']}\n"

        summary_msg = f"""{detailed_msg}
👇🏻 <b>Daily Trade Summary</b>
------------------------
Total Trades: {total_signals}
✔️ Profitable: {profitable}
✖️ Loss: {lost}
◼️ Open/Cancelled: {cancelled}
------------------------
📊 Net Profit: {net_pnl_percent}%"""
        send_telegram_message(summary_msg)
        trades.clear()  # Reset for next day

# ==============================
# 🔹 Background Thread
# ==============================
threading.Thread(target=send_daily_summary, daemon=True).start()
