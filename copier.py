import time
import json
import traceback
import logging
from datetime import datetime
import MetaTrader5 as mt5

STATE_FILE = "state.json"
LOG_FILE = "copier.log"

# ================= LOGGING =================
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

def log(msg):
    print(msg)
    logging.info(msg)

# ================= STATE =================
def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except:
        return {
            "last_signal_id": None,
            "processed_signals": []
        }

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

state = load_state()

# ================= MT5 INIT =================
def init_mt5():
    if not mt5.initialize():
        raise RuntimeError(f"MT5 init failed: {mt5.last_error()}")
    log("MT5 initialized")

# ================= TELEGRAM MOCK =================
def fetch_signals():
    """
    Replace with real Telegram polling logic
    Must return list of dicts:
    {id, symbol, action, lot, sl, tp}
    """
    return []

# ================= DUPLICATE PROTECTION =================
def already_processed(signal_id):
    return signal_id in state["processed_signals"]

# ================= EXECUTION =================
def execute_trade(signal):
    symbol = signal["symbol"]

    if not mt5.symbol_select(symbol, True):
        raise RuntimeError(f"Symbol select failed: {symbol}")

    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        raise RuntimeError("No tick data")

    price = tick.ask if signal["action"] == "BUY" else tick.bid

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": signal["lot"],
        "type": mt5.ORDER_TYPE_BUY if signal["action"] == "BUY" else mt5.ORDER_TYPE_SELL,
        "price": price,
        "sl": signal["sl"],
        "tp": signal["tp"],
        "deviation": 20,
        "magic": 777,
        "comment": f"copied_{signal['id']}",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    result = mt5.order_send(request)

    if result.retcode != mt5.TRADE_RETCODE_DONE:
        raise RuntimeError(f"Order failed: {result.retcode}")

    log(f"Trade executed: {signal['id']}")

# ================= SAFE LOOP =================
def run():
    init_mt5()

    consecutive_errors = 0

    while True:
        try:
            signals = fetch_signals()

            for signal in signals:
                sid = signal["id"]

                if already_processed(sid):
                    continue

                execute_trade(signal)

                state["processed_signals"].append(sid)
                save_state(state)

            consecutive_errors = 0
            time.sleep(1)

        except Exception as e:
            consecutive_errors += 1

            log(f"ERROR: {str(e)}")
            logging.error(traceback.format_exc())

            # Exponential backoff
            backoff = min(60, 2 ** consecutive_errors)
            log(f"Backoff {backoff}s")

            time.sleep(backoff)

# ================= ENTRY =================
if __name__ == "__main__":
    while True:
        try:
            run()
        except Exception as fatal:
            log("FATAL CRASH — restarting engine")
            logging.error(traceback.format_exc())
            time.sleep(5)
