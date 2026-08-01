import os
import time
import requests
import pandas as pd
import numpy as np

# ==========================================
# CONFIGURATION & ENVIRONMENT VARIABLES
# ==========================================
TWELVE_DATA_API_KEY = os.getenv("TWELVE_DATA_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

ASSETS = ["BTC/USD", "EUR/USD", "USD/JPY", "XAU/USD"]
MIN_CONFLUENCE_SCORE = 8  # Threshold out of 10 to trigger Telegram alert

# ==========================================
# DATA RETRIEVAL (TWELVE DATA)
# ==========================================
def fetch_ohlc(symbol, interval, outputsize=100):
    url = f"https://api.twelvedata.com/time_series?symbol={symbol}&interval={interval}&outputsize={outputsize}&apikey={TWELVE_DATA_API_KEY}"
    try:
        response = requests.get(url, timeout=10)
        data = response.json()
        if "values" not in data:
            print(f"Error fetching {symbol} ({interval}): {data.get('message', 'Unknown error')}")
            return None
        
        df = pd.DataFrame(data["values"])
        df['datetime'] = pd.to_datetime(df['datetime'])
        df = df.sort_values('datetime').reset_index(drop=True)
        for col in ['open', 'high', 'low', 'close', 'volume']:
            if col in df.columns:
                df[col] = df[col].astype(float)
        return df
    except Exception as e:
        print(f"Exception fetching {symbol} ({interval}): {e}")
        return None

# ==========================================
# SMC ANALYSIS & HTF FILTERS
# ==========================================
def get_4h_bias(df_4h):
    """ Determines HTF trend using 50 EMA and Market Structure """
    if df_4h is None or len(df_4h) < 50:
        return "NEUTRAL"
    
    close = df_4h['close'].iloc[-1]
    ema_50 = df_4h['close'].ewm(span=50).mean().iloc[-1]
    
    # Check Higher Highs / Higher Lows over recent candles
    recent_highs = df_4h['high'].tail(10).values
    recent_lows = df_4h['low'].tail(10).values
    
    is_hh_hl = recent_highs[-1] > recent_highs[-5] and recent_lows[-1] > recent_lows[-5]
    is_lh_ll = recent_highs[-1] < recent_highs[-5] and recent_lows[-1] < recent_lows[-5]
    
    if close > ema_50 and is_hh_hl:
        return "BULLISH"
    elif close < ema_50 and is_lh_ll:
        return "BEARISH"
    elif close > ema_50:
        return "WEAK_BULLISH"
    elif close < ema_50:
        return "WEAK_BEARISH"
    return "NEUTRAL"

def check_premium_discount(df_1h, current_price):
    """ Calculates 1H Range Equilibrium (Premium vs Discount) """
    if df_1h is None or len(df_1h) < 20:
        return "UNKNOWN"
    
    swing_high = df_1h['high'].tail(40).max()
    swing_low = df_1h['low'].tail(40).min()
    equilibrium = (swing_high + swing_low) / 2.0
    
    if current_price < equilibrium:
        return "DISCOUNT"  # Optimal for BUY
    else:
        return "PREMIUM"   # Optimal for SELL

def detect_15m_setup(df_15m, htf_bias, pd_zone):
    """ Analyzes 15m structural setups with strict confluence scoring """
    if df_15m is None or len(df_15m) < 15:
        return None

    current_price = df_15m['close'].iloc[-1]
    score = 0
    reasons = []
    direction = None

    # Determine Direction Alignment
    if "BULLISH" in htf_bias and pd_zone == "DISCOUNT":
        direction = "BUY"
        score += 3
        reasons.append("4H Bullish Trend + 1H Discount Zone (+3)")
    elif "BEARISH" in htf_bias and pd_zone == "PREMIUM":
        direction = "SELL"
        score += 3
        reasons.append("4H Bearish Trend + 1H Premium Zone (+3)")
    else:
        # Strictly reject if HTF and Premium/Discount are misaligned
        return None

    # Check Liquidity Sweep on 15m
    recent_low = df_15m['low'].tail(10).min()
    recent_high = df_15m['high'].tail(10).max()
    prev_low = df_15m['low'].iloc[-15:-5].min()
    prev_high = df_15m['high'].iloc[-15:-5].max()

    if direction == "BUY" and recent_low < prev_low and current_price > prev_low:
        score += 2
        reasons.append("15m Liquidity Sweep Below Swing Low (+2)")
    elif direction == "SELL" and recent_high > prev_high and current_price < prev_high:
        score += 2
        reasons.append("15m Liquidity Sweep Above Swing High (+2)")

    # Check Fair Value Gap (FVG)
    # FVG Buy: Candle 3 Low > Candle 1 High
    # FVG Sell: Candle 3 High < Candle 1 Low
    c1_high, c1_low = df_15m['high'].iloc[-3], df_15m['low'].iloc[-3]
    c3_high, c3_low = df_15m['high'].iloc[-1], df_15m['low'].iloc[-1]

    if direction == "BUY" and c3_low > c1_high:
        score += 3
        reasons.append("15m Bullish Fair Value Gap (FVG) (+3)")
    elif direction == "SELL" and c3_high < c1_low:
        score += 3
        reasons.append("15m Bearish Fair Value Gap (FVG) (+3)")

    # Check Order Block (OB) / Change of Character (CHoCH)
    ema_20 = df_15m['close'].ewm(span=20).mean().iloc[-1]
    if direction == "BUY" and current_price > ema_20:
        score += 2
        reasons.append("15m CHoCH Confirmation (+2)")
    elif direction == "SELL" and current_price < ema_20:
        score += 2
        reasons.append("15m CHoCH Confirmation (+2)")

    if score >= MIN_CONFLUENCE_SCORE:
        # Calculate Risk/Reward Levels
        atr = (df_15m['high'] - df_15m['low']).tail(14).mean()
        if direction == "BUY":
            sl = current_price - (atr * 1.5)
            risk = current_price - sl
            tp1 = current_price + (risk * 1.5)  # 1:1.5 RR (Break-Even / Partial Profit)
            tp2 = current_price + (risk * 3.0)  # 1:3 RR (Final Target)
        else:
            sl = current_price + (atr * 1.5)
            risk = sl - current_price
            tp1 = current_price - (risk * 1.5)
            tp2 = current_price - (risk * 3.0)

        return {
            "direction": direction,
            "score": score,
            "price": current_price,
            "sl": sl,
            "tp1": tp1,
            "tp2": tp2,
            "reasons": reasons
        }
    
    return None

# ==========================================
# TELEGRAM NOTIFIER
# ==========================================
def send_telegram_alert(symbol, setup):
    message = (
        f"🚨 **SMC TRADING ALERT: {setup['direction']} {symbol}** 🚨\n"
        f"----------------------------------------\n"
        f"🎯 **Confluence Score:** {setup['score']}/10\n"
        f"📍 **Entry Price:** {setup['price']:.5f}\n"
        f"🛑 **Stop Loss:** {setup['sl']:.5f}\n"
        f"⚡ **TP1 (1:1.5 RR - Lock BE):** {setup['tp1']:.5f}\n"
        f"🚀 **TP2 (1:3 RR - Target):** {setup['tp2']:.5f}\n\n"
        f"📋 **Confluence Factors:**\n"
        + "\n".join([f"• {r}" for r in setup['reasons']]) + "\n\n"
        f"📌 *Management:* Move SL to Entry (Break-Even) immediately upon hitting TP1!"
    )
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }
    try:
        res = requests.post(url, json=payload, timeout=10)
        if res.status_code == 200:
            print(f"Alert sent successfully for {symbol}")
        else:
            print(f"Failed to send alert: {res.text}")
    except Exception as e:
        print(f"Error sending Telegram alert: {e}")

# ==========================================
# MAIN EXECUTION
# ==========================================
def main():
    print("Starting SMC Multi-Asset Scanner...")
    
    for symbol in ASSETS:
        print(f"\nScanning {symbol}...")
        
        # 1. Fetch Timeframes
        df_4h = fetch_ohlc(symbol, "4h", outputsize=60)
        time.sleep(8.5)  # Stay within Twelve Data 8 req/min rate limit
        
        df_1h = fetch_ohlc(symbol, "1h", outputsize=60)
        time.sleep(8.5)
        
        df_15m = fetch_ohlc(symbol, "15min", outputsize=60)
        time.sleep(8.5)

        if df_4h is None or df_1h is None or df_15m is None:
            print(f"Skipping {symbol} due to missing data.")
            continue

        # 2. Higher Timeframe Alignment
        htf_bias = get_4h_bias(df_4h)
        current_price = df_15m['close'].iloc[-1]
        pd_zone = check_premium_discount(df_1h, current_price)

        print(f"[{symbol}] 4H Bias: {htf_bias} | 1H Zone: {pd_zone}")

        # 3. Detect 15m Setup
        setup = detect_15m_setup(df_15m, htf_bias, pd_zone)

        if setup:
            print(f"✅ Valid Setup Found for {symbol}! Sending Telegram Alert...")
            send_telegram_alert(symbol, setup)
        else:
            print(f"❌ No high-confluence setup for {symbol}.")

if __name__ == "__main__":
    main()
