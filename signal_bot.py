"""
Gold AI-Score Strategy -- standalone signal notifier.

Reimplements the Pine Script strategy's composite score, entry/exit rules,
and risk-management levels in Python so it can run for free on a schedule
(GitHub Actions) and send Telegram messages, completely independent of
TradingView's paid alert system.

Pulls closed 4h candles for XAUTUSDT (Bitget public API, no auth needed),
recomputes the same regime-adaptive score as gold-ai-score-backtest.pine,
and tracks a lightweight paper position in state.json so it knows whether
it's "in a trade" across runs (GitHub Actions runners are stateless --
state.json is committed back to the repo after each run).

IMPORTANT: GitHub's scheduler regularly skips runs. Because 4h candles
close every 4 hours, a run can easily miss one or more candles. The bot
therefore replays EVERY candle closed since the last processed one
instead of only looking at the newest candle -- otherwise a signal on a
skipped candle would be lost without a trace.

This sends notifications only. It never places real orders.
"""
import datetime as dt
import json
import os
import sys
import time
import urllib.request
import urllib.error

import numpy as np
import pandas as pd

try:
    from zoneinfo import ZoneInfo
    BERLIN = ZoneInfo("Europe/Berlin")
except Exception:
    BERLIN = None

# ---------------------------------------------------------------------------
# Parameters -- must match gold-ai-score-backtest.pine's tuned defaults
# ---------------------------------------------------------------------------
SYMBOL = "XAUTUSDT"
PRODUCT_TYPE = "usdt-futures"
GRANULARITY = "4H"
BAR_MS = 4 * 60 * 60 * 1000

EMA_FAST_LEN = 12
EMA_SLOW_LEN = 34
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9
RSI_LEN = 14
BB_LEN, BB_MULT = 20, 2.0
ADX_LEN = 14
ADX_FLOOR, ADX_CEIL = 15.0, 35.0
VOL_LEN = 20
ATR_LEN = 14

ENTRY_THRESHOLD = 0.75
ATR_STOP_MULT = 2.2
TP1_R, TP2_R = 1.5, 3.0
CHANDELIER_LEN, CHANDELIER_MULT = 22, 3.0
COOLDOWN_BARS = 4

# Replay at most this many missed candles (2 days). Anything older is
# replayed silently to rebuild the position state without spamming.
MAX_REPLAY_BARS = 12

STATE_FILE = os.path.join(os.path.dirname(__file__), "state.json")


def fetch_candles(limit=300):
    url = (
        f"https://api.bitget.com/api/v2/mix/market/candles"
        f"?symbol={SYMBOL}&granularity={GRANULARITY}&limit={limit}&productType={PRODUCT_TYPE}"
    )
    with urllib.request.urlopen(url, timeout=20) as resp:
        payload = json.loads(resp.read())
    if payload.get("code") != "00000":
        raise RuntimeError(f"Bitget API error: {payload}")
    rows = payload["data"]
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "baseVol", "quoteVol"])
    for col in ["ts", "open", "high", "low", "close", "baseVol"]:
        df[col] = pd.to_numeric(df[col])
    df = df.rename(columns={"baseVol": "volume"})
    df = df.sort_values("ts").reset_index(drop=True)
    now_ms = int(time.time() * 1000)
    df = df[df["ts"] + BAR_MS <= now_ms].reset_index(drop=True)
    return df


def ema(series, length):
    return series.ewm(span=length, adjust=False).mean()


def rma(series, length):
    return series.ewm(alpha=1.0 / length, adjust=False).mean()


def rsi(series, length):
    delta = series.diff()
    gain = rma(delta.clip(lower=0), length)
    loss = rma(-delta.clip(upper=0), length)
    rs = gain / loss
    return 100 - (100 / (1 + rs))


def true_range(df):
    prev_close = df["close"].shift(1)
    return pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)


def atr(df, length):
    return rma(true_range(df), length)


def adx(df, length):
    up_move = df["high"].diff()
    down_move = -df["low"].diff()
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    tr_rma = rma(true_range(df), length)
    plus_di = 100 * rma(pd.Series(plus_dm, index=df.index), length) / tr_rma
    minus_di = 100 * rma(pd.Series(minus_dm, index=df.index), length) / tr_rma
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    return rma(dx, length)


def compute_indicators(df):
    df = df.copy()
    df["ema_fast"] = ema(df["close"], EMA_FAST_LEN)
    df["ema_slow"] = ema(df["close"], EMA_SLOW_LEN)
    macd_line = ema(df["close"], MACD_FAST) - ema(df["close"], MACD_SLOW)
    df["macd_hist"] = macd_line - ema(macd_line, MACD_SIGNAL)
    df["rsi"] = rsi(df["close"], RSI_LEN)
    df["atr"] = atr(df, ATR_LEN)
    df["basis"] = df["close"].rolling(BB_LEN).mean()
    df["stddev"] = df["close"].rolling(BB_LEN).std(ddof=0)
    df["adx"] = adx(df, ADX_LEN)
    df["vol_sma"] = df["volume"].rolling(VOL_LEN).mean()
    df["chand_high"] = df["high"].rolling(CHANDELIER_LEN).max() - CHANDELIER_MULT * df["atr"]
    df["chand_low"] = df["low"].rolling(CHANDELIER_LEN).min() + CHANDELIER_MULT * df["atr"]

    trend_weight = ((df["adx"] - ADX_FLOOR) / (ADX_CEIL - ADX_FLOOR)).clip(0, 1)
    mr_weight = 1 - trend_weight
    ema_score = ((df["ema_fast"] - df["ema_slow"]) / df["atr"]).clip(-1, 1)
    macd_score = (df["macd_hist"] / df["atr"]).clip(-1, 1)
    trend_rsi_score = ((df["rsi"] - 50) / 25).clip(-1, 1)
    trend_score = (ema_score + macd_score + trend_rsi_score) / 3
    bb_score = ((df["basis"] - df["close"]) / (BB_MULT * df["stddev"])).clip(-1, 1)
    mr_rsi_score = ((50 - df["rsi"]) / 25).clip(-1, 1)
    mr_score = (bb_score + mr_rsi_score) / 2
    vol_boost = np.where(df["volume"] > df["vol_sma"], 0.1, -0.05)
    df["score"] = trend_weight * trend_score + mr_weight * mr_score + vol_boost
    return df


def load_state():
    default = {
        "side": None, "entry": None, "sl": None, "tp1": None, "tp2": None,
        "tp1_filled": False, "tp2_filled": False,
        "bars_since_sl": 999999, "last_processed_ts": 0,
    }
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            saved = json.load(f)
        default.update(saved)
    return default


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def send_telegram(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set -- skipping send, printing instead:")
        print(text)
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = json.dumps({"chat_id": chat_id, "text": text}).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
    except urllib.error.URLError as e:
        print(f"Telegram send failed: {e}", file=sys.stderr)


def fmt(x):
    return f"{x:.2f}"


def _local(d):
    return d.astimezone(BERLIN) if BERLIN is not None else d


def candle_footer(ts_ms):
    """Which candle this event belongs to + when the message actually went out,
    so any scheduler delay is visible instead of confusing."""
    closed = dt.datetime.fromtimestamp((ts_ms + BAR_MS) / 1000, dt.timezone.utc)
    now = dt.datetime.now(dt.timezone.utc)
    delay_min = int((now - closed).total_seconds() // 60)
    line = "\n\nKerze geschlossen: " + _local(closed).strftime("%d.%m.%Y %H:%M")
    line += "\nNachricht gesendet: " + _local(now).strftime("%d.%m.%Y %H:%M")
    if delay_min >= 5:
        h, m = divmod(delay_min, 60)
        line += f"  (Verzoegerung: {h}h {m}min)" if h else f"  (Verzoegerung: {m}min)"
    return line


def step(state, prev, cur, out):
    """Apply exactly one closed candle to the paper position. Mirrors the
    per-bar logic of the Gold AI-Score Pine strategy. Appends (ts, text)
    tuples to `out` for every event that should be notified."""
    ts = int(cur["ts"])
    state["bars_since_sl"] = min(state["bars_since_sl"] + 1, 999999)

    if state["side"] is not None:
        side = state["side"]
        if side == "long":
            if not state["tp1_filled"] and cur["high"] >= state["tp1"]:
                state["tp1_filled"] = True
                state["sl"] = state["entry"]
                out.append((ts,
                            f"Gold AI-Score: TP1 erreicht (Long)\nTP1: {fmt(state['tp1'])}\n"
                            f"Stop jetzt auf Einstieg (Break-even): {fmt(state['sl'])}"))
            elif state["tp1_filled"] and not state["tp2_filled"] and cur["high"] >= state["tp2"]:
                state["tp2_filled"] = True
                out.append((ts,
                            f"Gold AI-Score: TP2 erreicht (Long)\nTP2: {fmt(state['tp2'])}\n"
                            f"Rest laeuft mit Trailing-Stop weiter"))
            elif state["tp2_filled"] and cur["low"] <= cur["chand_low"]:
                out.append((ts, f"Gold AI-Score: Trailing-Stop ausgeloest (Long)\nExit: {fmt(cur['chand_low'])}"))
                state.update({"side": None, "entry": None, "sl": None, "tp1": None, "tp2": None,
                              "tp1_filled": False, "tp2_filled": False})
                state["bars_since_sl"] = 0
            elif cur["low"] <= state["sl"]:
                out.append((ts, f"Gold AI-Score: Stop-Loss ausgeloest (Long)\nExit: {fmt(state['sl'])}"))
                state.update({"side": None, "entry": None, "sl": None, "tp1": None, "tp2": None,
                              "tp1_filled": False, "tp2_filled": False})
                state["bars_since_sl"] = 0
        else:
            if not state["tp1_filled"] and cur["low"] <= state["tp1"]:
                state["tp1_filled"] = True
                state["sl"] = state["entry"]
                out.append((ts,
                            f"Gold AI-Score: TP1 erreicht (Short)\nTP1: {fmt(state['tp1'])}\n"
                            f"Stop jetzt auf Einstieg (Break-even): {fmt(state['sl'])}"))
            elif state["tp1_filled"] and not state["tp2_filled"] and cur["low"] <= state["tp2"]:
                state["tp2_filled"] = True
                out.append((ts,
                            f"Gold AI-Score: TP2 erreicht (Short)\nTP2: {fmt(state['tp2'])}\n"
                            f"Rest laeuft mit Trailing-Stop weiter"))
            elif state["tp2_filled"] and cur["high"] >= cur["chand_high"]:
                out.append((ts, f"Gold AI-Score: Trailing-Stop ausgeloest (Short)\nExit: {fmt(cur['chand_high'])}"))
                state.update({"side": None, "entry": None, "sl": None, "tp1": None, "tp2": None,
                              "tp1_filled": False, "tp2_filled": False})
                state["bars_since_sl"] = 0
            elif cur["high"] >= state["sl"]:
                out.append((ts, f"Gold AI-Score: Stop-Loss ausgeloest (Short)\nExit: {fmt(state['sl'])}"))
                state.update({"side": None, "entry": None, "sl": None, "tp1": None, "tp2": None,
                              "tp1_filled": False, "tp2_filled": False})
                state["bars_since_sl"] = 0

    if state["side"] is None and state["bars_since_sl"] >= COOLDOWN_BARS:
        bull_cross = prev["score"] <= ENTRY_THRESHOLD and cur["score"] > ENTRY_THRESHOLD
        bear_cross = prev["score"] >= -ENTRY_THRESHOLD and cur["score"] < -ENTRY_THRESHOLD
        close = cur["close"]
        atr_val = cur["atr"]

        if bull_cross:
            sl = close - ATR_STOP_MULT * atr_val
            risk = close - sl
            tp1 = close + TP1_R * risk
            tp2 = close + TP2_R * risk
            state.update({"side": "long", "entry": close, "sl": sl, "tp1": tp1, "tp2": tp2,
                          "tp1_filled": False, "tp2_filled": False})
            out.append((ts,
                        f"LONG SIGNAL - Gold AI-Score\nEntry: {fmt(close)}\nStop-Loss: {fmt(sl)}\n"
                        f"TP1 (40%): {fmt(tp1)}\nTP2 (50%): {fmt(tp2)}"))
        elif bear_cross:
            sl = close + ATR_STOP_MULT * atr_val
            risk = sl - close
            tp1 = close - TP1_R * risk
            tp2 = close - TP2_R * risk
            state.update({"side": "short", "entry": close, "sl": sl, "tp1": tp1, "tp2": tp2,
                          "tp1_filled": False, "tp2_filled": False})
            out.append((ts,
                        f"SHORT SIGNAL - Gold AI-Score\nEntry: {fmt(close)}\nStop-Loss: {fmt(sl)}\n"
                        f"TP1 (40%): {fmt(tp1)}\nTP2 (50%): {fmt(tp2)}"))

    state["last_processed_ts"] = ts


def main():
    df = fetch_candles(limit=300)
    warmup = max(EMA_SLOW_LEN, BB_LEN, ADX_LEN * 2, CHANDELIER_LEN) + 10
    if len(df) < warmup:
        print(f"Not enough candles yet ({len(df)} < {warmup}), skipping this run.")
        return

    df = compute_indicators(df)
    state = load_state()

    last_ts = int(df["ts"].iloc[-1])
    if last_ts <= state["last_processed_ts"]:
        print("No new closed candle since last run, nothing to do.")
        return

    # First run ever: don't replay history, just anchor on the newest candle.
    if state["last_processed_ts"] == 0:
        state["last_processed_ts"] = last_ts
        save_state(state)
        print(f"First run -- anchored on candle {last_ts}, no notifications sent.")
        return

    pending = df.index[df["ts"] > state["last_processed_ts"]].tolist()
    pending = [i for i in pending if i >= 1]
    if not pending:
        print("Nothing pending.")
        return

    if len(pending) > MAX_REPLAY_BARS:
        skipped = pending[:-MAX_REPLAY_BARS]
        pending = pending[-MAX_REPLAY_BARS:]
        print(f"Gap too large -- replaying {len(skipped)} old candles silently.")
        for i in skipped:
            step(state, df.iloc[i - 1], df.iloc[i], [])

    print(f"Replaying {len(pending)} candle(s): "
          f"{[int(df['ts'].iloc[i]) for i in pending]}")

    events = []
    for i in pending:
        step(state, df.iloc[i - 1], df.iloc[i], events)

    for ts, text in events:
        send_telegram(text + candle_footer(ts))

    save_state(state)
    print(f"Run complete. {len(events)} notification(s). State: {state}")


if __name__ == "__main__":
    main()
