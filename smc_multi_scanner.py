import os
import requests
import yfinance as yf
import pandas as pd

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

ASSETS = {
    "EUR/USD": "EURUSD=X",
    "XAU/USD (Gold)": "GC=F",
    "BTC/USD": "BTC-USD",
    "ETH/USD": "ETH-USD",
    "USD/JPY": "JPY=X",
    "GBP/JPY": "GBPJPY=X",
    "XAG/USD (Silver)": "SI=F"
}

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    requests.post(url, data=payload)

def analyze_top_down_smc(symbol_name, ticker):
    # Fetch 4H HTF Data (60 days) and 15m LTF Data (5 days)
    df_4h = yf.download(ticker, period="60d", interval="1h", progress=False)
    df_15m = yf.download(ticker, period="5d", interval="15m", progress=False)

    if len(df_4h) < 50 or len(df_15m) < 30:
        return

    # --- STEP 1: 4H HTF BIAS ANALYSIS ---
    htf_high = df_4h['High'].iloc[-20:-2].max()
    htf_low = df_4h['Low'].iloc[-20:-2].min()
    htf_close = df_4h['Close'].iloc[-1]

    htf_bullish = htf_close > (htf_high + htf_low) / 2
    htf_bearish = not htf_bullish

    # --- STEP 2: 15M LTF SMC ENTRY ---
    df_15m['ATR'] = (df_15m['High'] - df_15m['Low']).rolling(14).mean()
    
    c0_close = df_15m['Close'].iloc[-1]
    c1_high, c1_low, c1_close = df_15m['High'].iloc[-2], df_15m['Low'].iloc[-2], df_15m['Close'].iloc[-2]
    c2_high, c2_low = df_15m['High'].iloc[-3], df_15m['Low'].iloc[-3]
    c3_high, c3_low = df_15m['High'].iloc[-4], df_15m['Low'].iloc[-4]
    atr = df_15m['ATR'].iloc[-1]

    swing_high_15m = df_15m['High'].iloc[-22:-2].max()
    swing_low_15m = df_15m['Low'].iloc[-22:-2].min()

    # 15m Sweeps
    bull_sweep = (c1_low < swing_low_15m) and (c1_close > swing_low_15m)
    bear_sweep = (c1_high > swing_high_15m) and (c1_close < swing_high_15m)

    # 15m Market Structure Shift (MSS)
    bull_mss = bull_sweep and (c1_close > c2_high)
    bear_mss = bear_sweep and (c1_close < c2_low)

    # 15m FVG / Imbalance
    bull_fvg = c1_low > c3_high
    bear_fvg = c1_high < c3_low

    # Confluence: LTF Setup MUST align with HTF Context
    valid_buy = htf_bullish and bull_mss and bull_fvg
    valid_sell = htf_bearish and bear_mss and bear_fvg

    if valid_buy or valid_sell:
        direction = "BUY SMC (4H + 15M)" if valid_buy else "SELL SMC (4H + 15M)"
        sl = (c1_low - (atr * 0.2)) if valid_buy else (c1_high + (atr * 0.2))
        risk = abs(c0_close - sl)
        tp = (c0_close + (risk * 3.0)) if valid_buy else (c0_close - (risk * 3.0))

        msg = (f"🌐 TOP-DOWN SMC ALERT 🌐\n\n"
               f"Asset: {symbol_name}\nDirection: {direction}\nPrice: {round(c0_close, 4)}\n\n"
               f"✓ 4H HTF Alignment Confirmed\n"
               f"✓ 15m Liquidity Sweep\n"
               f"✓ 15m Market Structure Shift\n"
               f"✓ 15m Order Block / FVG Imbalance\n\n"
               f"📍 Entry: Market / FVG Refinement\n🛡️ Stop Loss: {round(sl, 4)}\n🎯 Take Profit (1:3): {round(tp, 4)}")
        send_telegram(msg)

for name, ticker_code in ASSETS.items():
    try:
        analyze_top_down_smc(name, ticker_code)
    except Exception as e:
        print(f"Error processing {name}: {e}")
