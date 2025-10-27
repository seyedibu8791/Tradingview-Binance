# trade_notifier.py

import requests
import threading
import datetime
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TRADE_AMOUNT, LEVERAGE

# ===== In-memory trade storage =====
trades = {}  # Stores ongoing and closed trades

# ===== Telegram Helper =====
def send_telegram_message(message):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
        requests.post(url, data=payload)
    except Exception as e:
        print("❌ Telegram send failed:", e)

# ===== Log Entry =====
def log_trade_entry(symbol, side, order_id, filled_price):
    trades[symbol] = {
        "side": side,
        "entry_price": filled_price,
        "order_id": order_id,
        "closed": False,
        "exit_price": None,
        "pnl": 0,
        "pnl_percent": 0
    }
    message = f"""💹 <b>New Trade Entry</b>
Symbol: #{symbol}
Side: {side}
Leverage: {LEVERAGE}X
Entry Price: {filled_price}
Amount: ${TRADE_AMOUNT}
⚠️ Waiting for exit signal..."""
    send_telegram_message(message)

# ===== Log Exit =====
def log_trade_exit(symbol, order_id, filled_price):
    if symbol not in trades:
        return

    trade = trades[symbol]
    trade["exit_price"] = filled_price
    trade["closed"] = True

    # Calculate PnL with leverage
    qty = TRADE_AMOUNT
    if trade["side"] == "BUY":
        pnl = (filled_price - trade["entry_price"]) * qty * LEVERAGE / trade["entry_price"]
        pnl_percent = ((filled_price - trade["entry_price"]) / trade["entry_price"]) * 100 * LEVERAGE
    else:
        pnl = (trade["entry_price"] - filled_price) * qty * LEVERAGE / trade["entry_price"]
        pnl_percent = ((trade["entry_price"] - filled_price) / trade["entry_price"]) * 100 * LEVERAGE

    trade["pnl"] = round(pnl, 2)
    trade["pnl_percent"] = round(pnl_percent, 2)

    status_icon = "✅" if pnl > 0 else "⛔️"
    message = f"""💰 <b>Trade Closed</b> {status_icon}
Symbol: #{symbol}
Side: {trade['side']}
Entry Price: {trade['entry_price']}
Exit Price: {trade['exit_price']}
PnL $: {trade['pnl']}
PnL %: {trade['pnl_percent']}%"""
    send_telegram_message(message)

# ===== Daily Summary =====
def send_daily_summary():
    while True:
        now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5.5)))  # IST
        next_run = now.replace(hour=0, minute=0, second=0, microsecond=0) + datetime.timedelta(days=1)
        sleep_seconds = (next_run - now).total_seconds()
        threading.Event().wait(sleep_seconds)

        total_signals = len(trades)
        profitable = sum(1 for t in trades.values() if t["closed"] and t["pnl"] > 0)
        lost = sum(1 for t in trades.values() if t["closed"] and t["pnl"] < 0)
        cancelled = sum(1 for t in trades.values() if not t["closed"])
        net_pnl_percent = round(sum(t["pnl_percent"] for t in trades.values() if t["closed"]), 2)

        detailed_msg = ""
        for t_sym, t in trades.items():
            if t["closed"]:
                status_icon = "✅" if t["pnl"] > 0 else "⛔️"
                detailed_msg += f"#{t_sym} {t['side']} Closed {status_icon} | Entry: {t['entry_price']} | Exit: {t['exit_price']} | PnL%: {t['pnl_percent']} | PnL$: {t['pnl']}\n"

        summary_msg = f"""📊 <b>Daily Trade Summary</b>
{detailed_msg}
👇 Signals Summary
➕ Total Signals Sent: {total_signals}
✔️ Profitable: {profitable}
✖️ Lost: {lost}
◼️ Cancelled: {cancelled}
✅ Net PnL: {net_pnl_percent}%"""
        send_telegram_message(summary_msg)
        trades.clear()  # Reset trades for next day

# ===== Start Daily Summary Thread =====
threading.Thread(target=send_daily_summary, daemon=True).start()
