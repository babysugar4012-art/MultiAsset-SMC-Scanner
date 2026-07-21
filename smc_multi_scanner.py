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

def analyze_smc(symbol_name, ticker):
    df = yf.download(ticker, period="5d", interval="15m", progress=False)
    if len(df) < 30:
        return

    df['ATR'] = (df['High'] - df['Low']).rolling(14).mean()
    
    c0_close, c0_open = df['Close'].iloc[-1], df['Open'].iloc[-1]
    c1_high, c1_low, c1_close = df['High'].iloc[-2], df['Low'].iloc[-2], df['Close'].iloc[-2]
    c2_high, c2_low = df['High'].iloc[-3], df['Low'].iloc[-3]
    c3_high, c3_low = df['High'].iloc[-4], df['Low'].iloc[-4]
    atr = df['ATR'].iloc[-1]

    swing_high = df['High'].iloc[-22:-2].max()
    swing_low = df['Low'].iloc[-22:-2].min()

    bull_sweep = (c1_low < swing_low) and (c1_close > swing_low)
    bear_sweep = (c1_high > swing_high) and (c1_close < swing_high)

    bull_mss = bull_sweep and (c1_close > c2_high)
    bear_mss = bear_sweep and (c1_close < c2_low)

    bull_fvg = c1_low > c3_high
    bear_fvg = c1_high < c3_low

    valid_buy = bull_mss and bull_fvg
    valid_sell = bear_mss and bear_fvg

    if valid_buy or valid_sell:
        direction = "BUY SMC" if valid_buy else "SELL SMC"
        sl = (c1_low - (atr * 0.2)) if valid_buy else (c1_high + (atr * 0.2))
        risk = abs(c0_close - sl)
        tp = (c0_close + (risk * 3.0)) if valid_buy else (c0_close - (risk * 3.0))

        msg = (f"🌐 MULTI-ASSET SMC ALERT 🌐\n\n"
               f"Asset: {symbol_name}\nAction: {direction}\nPrice: {round(c0_close, 4)}\n\n"
               f"📍 Entry: Market / FVG\n🛡️ Stop Loss: {round(sl, 4)}\n🎯 Take Profit (1:3): {round(tp, 4)}\n\n"
               f"✓ Session Sweep\n✓ Market Structure Shift\n✓ FVG Imbalance")
        send_telegram(msg)

for name, ticker_code in ASSETS.items():
    try:
        analyze_smc(name, ticker_code)
    except Exception as e:
        print(f"Error processing {name}: {e}")
