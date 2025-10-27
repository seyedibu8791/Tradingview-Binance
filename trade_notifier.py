# trade_notifier.py

import requests, threading, time, datetime, os, logging
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TRADE_AMOUNT, LEVERAGE

# ==============================
# 🔹 Logger Setup
# ==============================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("trade_notifier.log"), logging.StreamHandler()]
)

# ==============================
# 🔹 Trade Store
# ==============================
trades = {}  # Keeps record of ongoing and closed trades

# ==============================
# 🔹 Telegram Helper
# ==============================
def send_telegram_message(message: str):
    """Send formatted message to Telegram channel or user."""
    try:
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            logging.warning("⚠️ Telegram credentials missing — skipping message send.")
            return

        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
        response = requests.post(url, data=payload, timeout=10)

        if response.status_code != 200:
            logging.error(f"❌ Telegram send failed: {response.text}")
        else:
            logging.info("📨 Message sent to Telegram successfully.")

    except Exception as e:
        logging.exception(f"❌ Exception sending Telegram message: {e}")

# ==============================
# 🔹 Log Trade Entry
# ==============================
def log_trade_entry(symbol, side, order_id, filled_price):
    """Record and notify a new trade entry."""
    trades[symbol] = {
        "side": side,
        "entry_price": filled_price,
        "order_id": order_id,
        "closed": False,
        "exit_price": None,
        "pnl": 0,
        "pnl_percent": 0
    }

    message = f"""📈 <b>Trade Entry</b>
Symbol: <b>#{symbol}</b>
Action: <b>{side}</b>
Leverage: {LEVERAGE}x
Entry Price: {filled_price}
Trade Amount: ${TRADE_AMOUNT}
──────────────
🕐 Wait for Exit Signal!"""
    logging.info(f"✅ Logged entry for {symbol} ({side}) at {filled_price}")
    send_telegram_message(message)

# ==============================
# 🔹 Log Trade Exit
# ==============================
def log_trade_exit(symbol, order_id, filled_price):
    """Record and notify trade exit and compute PnL."""
    if symbol not in trades:
        logging.warning(f"⚠️ No entry record found for {symbol}. Skipping exit log.")
        return

    trade = trades[symbol]
    trade["exit_price"] = filled_price
    trade["closed"] = True

    # Calculate PnL
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
    message = f"""📉 <b>Trade Closed</b> {status_icon}
Symbol: <b>#{symbol}</b>
Side: {trade['side']}
──────────────
Entry: {trade['entry_price']}
Exit: {trade['exit_price']}
──────────────
PnL: <b>{trade['pnl_percent']}%</b> | ${trade['pnl']}"""
    logging.info(f"📊 Closed {symbol} ({trade['side']}) | PnL {trade['pnl_percent']}% (${trade['pnl']})")
    send_telegram_message(message)

# ==============================
# 🔹 Daily Summary Thread
# ==============================
def send_daily_summary():
    """Send a daily trade summary at midnight IST."""
    while True:
        now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5.5)))  # IST
        next_run = now.replace(hour=0, minute=0, second=0, microsecond=0) + datetime.timedelta(days=1)
        sleep_seconds = (next_run - now).total_seconds()
        logging.info(f"🕛 Sleeping {sleep_seconds/3600:.2f} hrs until next summary send.")
        time.sleep(sleep_seconds)

        total_signals = len(trades)
        profitable = sum(1 for t in trades.values() if t["closed"] and t["pnl"] > 0)
        lost = sum(1 for t in trades.values() if t["closed"] and t["pnl"] < 0)
        cancelled = sum(1 for t in trades.values() if not t["closed"])
        net_pnl_percent = round(sum(t["pnl_percent"] for t in trades.values() if t["closed"]), 2)

        detailed_msg = ""
        for sym, t in trades.items():
            if t["closed"]:
                status_icon = "✅" if t["pnl"] > 0 else "⛔️"
                detailed_msg += f"#{sym} {t['side']} {status_icon} | Entry: {t['entry_price']} | Exit: {t['exit_price']} | {t['pnl_percent']}% | ${t['pnl']}\n"

        summary_msg = f"""📊 <b>Daily Summary</b>
──────────────
{detailed_msg if detailed_msg else 'No closed trades yet.'}
──────────────
Total Trades: {total_signals}
Profitable: {profitable}
Lost: {lost}
Cancelled: {cancelled}
──────────────
Net PnL: <b>{net_pnl_percent}%</b>"""
        send_telegram_message(summary_msg)
        trades.clear()  # Reset for next day
        logging.info("🔁 Daily summary sent & trade log reset.")

# Start daily summary thread
threading.Thread(target=send_daily_summary, daemon=True).start()
