import os
import time
import json
import requests
import pandas as pd
from datetime import datetime, timedelta, time as dt_time

# ENVIRONMENT SECRETS
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TWELVE_DATA_API_KEY = os.getenv("TWELVE_DATA_API_KEY")

# 1. OPTIMIZED 4-ASSET WATCHLIST
ASSETS = {
    "EUR/USD": "EUR/USD",
    "USD/JPY": "USD/JPY",
    "XAU/USD (Gold)": "XAU/USD",
    "BTC/USD": "BTC/USD"
}

# MAXIMUM ALLOWED STOP LOSS CAP PER ASSET (Prevents Wide/Bloated SL)
MAX_SL_CAPS = {
    "EUR/USD": 0.0015,       # Max 15 pips
    "USD/JPY": 0.18,         # Max 18 pips
    "XAU/USD (Gold)": 4.50,  # Max $4.50 Gold distance
    "BTC/USD": 180.0         # Max $180 BTC distance
}

STATE_FILE = "last_alerts.json"
COOLDOWN_HOURS = 3  # 3-Hour memory window per pair

# --- STATE MANAGEMENT FOR COOLDOWN ---
def load_alert_history():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_alert_history(history):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(history, f, indent=2)
    except Exception as e:
        print("Error saving state file:", e)

def should_send_alert(symbol_name):
    history = load_alert_history()
    if symbol_name in history:
        last_time = datetime.fromisoformat(history[symbol_name])
        if datetime.utcnow() - last_time < timedelta(hours=COOLDOWN_HOURS):
            print(f"⏳ {symbol_name}: Cooldown active. Alert sent at {last_time.strftime('%H:%M UTC')}. Skipping duplicate.")
            return False
    return True

def record_alert_sent(symbol_name):
    history = load_alert_history()
    history[symbol_name] = datetime.utcnow().isoformat()
    save_alert_history(history)

# --- TELEGRAM NOTIFIER ---
def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("❌ Telegram tokens missing.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    try:
        res = requests.post(url, data=payload)
        print("Telegram Sent Status:", res.status_code)
    except Exception as e:
        print("Telegram Error:", e)

def is_in_killzone():
    now_utc = datetime.utcnow().time()
    return dt_time(6, 0) <= now_utc <= dt_time(20, 0)

# --- TWELVE DATA API FETCH ---
def fetch_twelve_data(symbol, interval, outputsize):
    if not TWELVE_DATA_API_KEY:
        print("❌ TWELVE_DATA_API_KEY secret is missing!")
        return pd.DataFrame()

    url = f"https://api.twelvedata.com/time_series?symbol={symbol}&interval={interval}&outputsize={outputsize}&apikey={TWELVE_DATA_API_KEY}"
    try:
        response = requests.get(url).json()
        if "values" not in response:
            print(f"⚠️ Twelve Data Error for {symbol} ({interval}): {response.get('message', 'No data returned')}")
            return pd.DataFrame()

        df = pd.DataFrame(response["values"])
        df['datetime'] = pd.to_datetime(df['datetime'])
        df = df.sort_values('datetime').reset_index(drop=True)
        
        for col in ['open', 'high', 'low', 'close']:
            df[col] = df[col].astype(float)
            
        df.rename(columns={'open': 'Open', 'high': 'High', 'low': 'Low', 'close': 'Close'}, inplace=True)
        return df
    except Exception as e:
        print(f"Error fetching {symbol} from Twelve Data: {e}")
        return pd.DataFrame()

# --- INSTITUTIONAL SMC OTE PRECISION ENGINE ---
def analyze_institutional_smc(symbol_name, symbol_code):
    print(f"\n--- Analyzing {symbol_name} ---")

    # Skip Forex & Gold outside active Kill Zone hours
    if "BTC" not in symbol_name:
        if not is_in_killzone():
            print(f"⏳ {symbol_name}: Outside London/NY Kill Zone hours. Skipping...")
            return

    # 1. Fetch Data with Rate-Limit Padding (15s delay safely stays under Twelve Data limits)
    df_4h = fetch_twelve_data(symbol_code, "4h", 40)
    time.sleep(15)

    df_1h = fetch_twelve_data(symbol_code, "1h", 40)
    time.sleep(15)

    df_15m = fetch_twelve_data(symbol_code, "15min", 40)
    time.sleep(15)

    if df_4h.empty or df_1h.empty or df_15m.empty:
        print(f"⚠️ Skipping {symbol_name} due to incomplete candle data.")
        return

    # --- TIER 1: 4H MACRO TREND (BOS) ---
    h4_swing_high = float(df_4h['High'].iloc[-15:-2].max())
    h4_swing_low = float(df_4h['Low'].iloc[-15:-2].min())
    h4_close = float(df_4h['Close'].iloc[-1])

    h4_bullish = h4_close > h4_swing_high
    h4_bearish = h4_close < h4_swing_low

    if not h4_bullish and not h4_bearish:
        h4_bullish = h4_close > (h4_swing_high + h4_swing_low) / 2
        h4_bearish = not h4_bullish

    # --- TIER 2: 1H LIQUIDITY & SUPPLY/DEMAND ---
    h1_swing_high = float(df_1h['High'].iloc[-12:-2].max())
    h1_swing_low = float(df_1h['Low'].iloc[-12:-2].min())
    h1_close = float(df_1h['Close'].iloc[-1])

    h1_bsl_swept = float(df_1h['High'].iloc[-2]) > h1_swing_high
    h1_ssl_swept = float(df_1h['Low'].iloc[-2]) < h1_swing_low

    # --- TIER 3: 15M DISPLACEMENT & OTE RETRACEMENT (0.705 FIB + FVG) ---
    df_15m['ATR'] = (df_15m['High'] - df_15m['Low']).rolling(14).mean()
    atr = float(df_15m['ATR'].iloc[-1])

    c0_close = float(df_15m['Close'].iloc[-1])
    c1_high = float(df_15m['High'].iloc[-2])
    c1_low = float(df_15m['Low'].iloc[-2])
    c1_close = float(df_15m['Close'].iloc[-2])

    c2_high = float(df_15m['High'].iloc[-3])
    c2_low = float(df_15m['Low'].iloc[-3])
    c2_open = float(df_15m['Open'].iloc[-3])
    c2_close_val = float(df_15m['Close'].iloc[-3])

    c3_high = float(df_15m['High'].iloc[-4])
    c3_low = float(df_15m['Low'].iloc[-4])

    m15_bull_fvg = c1_low > c3_high
    m15_bear_fvg = c1_high < c3_low

    m15_bull_ob = (c2_close_val < c2_open) and (c1_close > c2_high)
    m15_bear_ob = (c2_close_val > c2_open) and (c1_close < c2_low)

    valid_buy = h4_bullish and (h1_ssl_swept or c0_close > h1_swing_low) and (m15_bull_fvg or m15_bull_ob)
    valid_sell = h4_bearish and (h1_bsl_swept or c0_close < h1_swing_high) and (m15_bear_fvg or m15_bear_ob)

    h4_str = "BULLISH" if h4_bullish else "BEARISH"
    print(f"🔍 {symbol_name} | Price: {c0_close:.4f} | 4H Bias: {h4_str} | Setup: {'YES' if (valid_buy or valid_sell) else 'NO SETUP'}")

    if valid_buy or valid_sell:
        if not should_send_alert(symbol_name):
            return

        direction = "INSTITUTIONAL BUY" if valid_buy else "INSTITUTIONAL SELL"

        # TIGHT OB WICK SL + MINIMAL ATR BUFFER
        buffer = atr * 0.25

        if valid_buy:
            ob_wick = min(c2_low, c1_low)
            tight_sl = ob_wick - buffer
            impulse_low = min(c1_low, c0_close)
            impulse_high = max(c2_high, c1_high)
            # OTE Entry at 0.705 Fib level inside the FVG/OB Zone
            ote_entry = impulse_high - ((impulse_high - impulse_low) * 0.705)
            entry_price = min(c0_close, ote_entry)
            risk = abs(entry_price - tight_sl)
            tp_12 = entry_price + (risk * 2.0)
            tp_14 = entry_price + (risk * 4.0)
        else:
            ob_wick = max(c2_high, c1_high)
            tight_sl = ob_wick + buffer
            impulse_high = max(c1_high, c0_close)
            impulse_low = min(c2_low, c1_low)
            # OTE Entry at 0.705 Fib level
            ote_entry = impulse_low + ((impulse_high - impulse_low) * 0.705)
            entry_price = max(c0_close, ote_entry)
            risk = abs(tight_sl - entry_price)
            tp_12 = entry_price - (risk * 2.0)
            tp_14 = entry_price - (risk * 4.0)

        # CHECK SL AGAINST MAXIMUM ALLOWED SL CAP
        max_cap = MAX_SL_CAPS.get(symbol_name, 9999)
        if risk > max_cap:
            print(f"⚠️ {symbol_name}: Risk ({risk:.4f}) exceeds Max Cap ({max_cap}). Discarding wide setup.")
            return

        dec = 2 if ("XAU" in symbol_name or "BTC" in symbol_name or "JPY" in symbol_name) else 4

        msg = (f"⚡ HIGH-PRECISION SMC OTE ALERT ⚡\n\n"
               f"Asset: {symbol_name}\nDirection: {direction}\n\n"
               f"✓ 4H Trend Aligned ({h4_str})\n"
               f"✓ 1H Liquidity & OB Active\n"
               f"✓ 15m OTE (0.705 Fib + FVG) Precision Entry\n"
               f"✓ Tight OB Wick SL Applied\n\n"
               f"📍 Entry Level: {round(entry_price, dec)}\n"
               f"🛡️ Tight SL: {round(tight_sl, dec)} (Risk: {round(risk, dec)})\n"
               f"🎯 TP1 (1:2 RR - Move SL to BE): {round(tp_12, dec)}\n"
               f"🎯 TP2 (1:4 RR - Main Target): {round(tp_14, dec)}")
        
        send_telegram(msg)
        record_alert_sent(symbol_name)

# --- SCRIPT EXECUTION ---
print(f"--- STARTING SMC MULTI-TIMEFRAME SCAN AT {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')} ---")
for name, ticker_code in ASSETS.items():
    try:
        analyze_institutional_smc(name, ticker_code)
    except Exception as e:
        print(f"Error processing {name}: {e}")
print("--- SCAN COMPLETE ---")
