import os
import sys
import time
import requests
import pyotp
import pandas as pd
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


# ============================================================
# CONFIGURATION
# ============================================================

IST = ZoneInfo("Asia/Kolkata")

DHAN_CLIENT_ID = os.environ["DHAN_CLIENT_ID"]
DHAN_PIN = os.environ["DHAN_PIN"]
DHAN_TOTP_SECRET = os.environ["DHAN_TOTP_SECRET"]

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

DHAN_BASE = "https://api.dhan.co/v2"
DHAN_AUTH = "https://auth.dhan.co"

INSTRUMENT_URL = (
    "https://images.dhan.co/api-data/api-scrip-master-detailed.csv"
)

# Current top 10 Nifty stocks by weight.
# We use these only to calculate the percentage above/below VWAP.
TOP_10 = [
    "HDFCBANK",
    "ICICIBANK",
    "RELIANCE",
    "BHARTIARTL",
    "LT",
    "SBIN",
    "INFY",
    "AXISBANK",
    "KOTAKBANK",
    "M&M",
]


# ============================================================
# HELPERS
# ============================================================

def fail(message):
    print(f"ERROR: {message}")
    sys.exit(1)


def dhan_headers(access_token):
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "access-token": access_token,
        "client-id": DHAN_CLIENT_ID,
    }


# ============================================================
# DHAN AUTHENTICATION
# ============================================================

def generate_access_token():
    """
    Dhan generates a fresh 24-hour access token when TOTP is enabled.
    """

    totp = pyotp.TOTP(DHAN_TOTP_SECRET).now()

    url = f"{DHAN_AUTH}/app/generateAccessToken"

    params = {
        "dhanClientId": DHAN_CLIENT_ID,
        "pin": DHAN_PIN,
        "totp": totp,
    }

    response = requests.post(
        url,
        params=params,
        timeout=20
    )

    if response.status_code != 200:
        fail(
            f"Dhan authentication failed: "
            f"{response.status_code} {response.text}"
        )

    data = response.json()

    access_token = data.get("accessToken")

    if not access_token:
        fail(f"No access token returned by Dhan: {data}")

    print("Dhan authentication successful.")

    return access_token


# ============================================================
# DOWNLOAD DHAN INSTRUMENT MASTER
# ============================================================

def load_instruments():
    print("Downloading Dhan instrument master...")

    try:
        df = pd.read_csv(
            INSTRUMENT_URL,
            low_memory=False
        )
    except Exception as e:
        fail(f"Could not download Dhan instrument list: {e}")

    print(f"Loaded {len(df)} instruments.")

    return df


# ============================================================
# FIND EQUITY SECURITY ID
# ============================================================

def find_equity(df, symbol):
    """
    Find NSE equity instrument for a stock.
    """

    possible_symbol_columns = [
        "SYMBOL_NAME",
        "SM_SYMBOL_NAME",
        "SEM_TRADING_SYMBOL",
        "DISPLAY_NAME",
        "SEM_CUSTOM_SYMBOL",
    ]

    symbol_col = None

    for col in possible_symbol_columns:
        if col in df.columns:
            symbol_col = col
            break

    if symbol_col is None:
        fail("Could not find symbol column in Dhan instrument master.")

    exchange_col = (
        "EXCH_ID"
        if "EXCH_ID" in df.columns
        else "SEM_EXM_EXCH_ID"
    )

    segment_col = (
        "SEGMENT"
        if "SEGMENT" in df.columns
        else "SEM_SEGMENT"
    )

    instrument_col = (
        "INSTRUMENT"
        if "INSTRUMENT" in df.columns
        else "SEM_INSTRUMENT_NAME"
    )

    security_col = (
        "SECURITY_ID"
        if "SECURITY_ID" in df.columns
        else "SEM_SMST_SECURITY_ID"
    )

    x = df[
        (df[exchange_col].astype(str).str.upper() == "NSE") &
        (df[segment_col].astype(str).str.upper() == "E") &
        (df[instrument_col].astype(str).str.upper() == "EQUITY") &
        (df[symbol_col].astype(str).str.upper() == symbol.upper())
    ]

    if x.empty:
        fail(f"Could not find NSE equity: {symbol}")

    return str(x.iloc[0][security_col])


# ============================================================
# FIND CURRENT INDEX FUTURE
# ============================================================

def find_current_future(df, underlying):
    """
    Find the nearest active futures contract for NIFTY or SENSEX.
    """

    exchange = "NSE" if underlying == "NIFTY" else "BSE"

    exchange_col = (
        "EXCH_ID"
        if "EXCH_ID" in df.columns
        else "SEM_EXM_EXCH_ID"
    )

    segment_col = (
        "SEGMENT"
        if "SEGMENT" in df.columns
        else "SEM_SEGMENT"
    )

    instrument_col = (
        "INSTRUMENT"
        if "INSTRUMENT" in df.columns
        else "SEM_INSTRUMENT_NAME"
    )

    underlying_col = "UNDERLYING_SYMBOL"

    security_col = (
        "SECURITY_ID"
        if "SECURITY_ID" in df.columns
        else "SEM_SMST_SECURITY_ID"
    )

    expiry_col = "SM_EXPIRY_DATE"

    if expiry_col not in df.columns:
        expiry_col = "EXPIRY_DATE"

    if underlying_col not in df.columns:
        fail("Dhan instrument file has no UNDERLYING_SYMBOL column.")

    if expiry_col not in df.columns:
        fail("Dhan instrument file has no expiry-date column.")

    x = df[
        (df[exchange_col].astype(str).str.upper() == exchange) &
        (df[segment_col].astype(str).str.upper() == "D") &
        (
            df[instrument_col]
            .astype(str)
            .str.upper()
            .isin(["FUTIDX", "FUTURE", "FUTIDX"])
        ) &
        (
            df[underlying_col]
            .astype(str)
            .str.upper()
            == underlying.upper()
        )
    ].copy()

    if x.empty:
        fail(f"No futures found for {underlying}.")

    x["expiry_parsed"] = pd.to_datetime(
        x[expiry_col],
        errors="coerce"
    )

    now = datetime.now(IST)

    # Keep only contracts which have not expired.
    x = x[
        x["expiry_parsed"].notna() &
        (
            x["expiry_parsed"].dt.date
            >= now.date()
        )
    ]

    if x.empty:
        fail(f"No active futures contract found for {underlying}.")

    x = x.sort_values("expiry_parsed")

    row = x.iloc[0]

    security_id = str(row[security_col])
    expiry = row["expiry_parsed"].date()

    print(
        f"{underlying} future: "
        f"security_id={security_id}, expiry={expiry}"
    )

    return security_id


# ============================================================
# HISTORICAL 1-MINUTE DATA
# ============================================================

def get_intraday_data(
    access_token,
    security_id,
    exchange_segment,
    instrument,
    start_dt,
    end_dt
):

    url = f"{DHAN_BASE}/charts/intraday"

    payload = {
        "securityId": str(security_id),
        "exchangeSegment": exchange_segment,
        "instrument": instrument,
        "interval": "1",
        "oi": False,
        "fromDate": start_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "toDate": end_dt.strftime("%Y-%m-%d %H:%M:%S"),
    }

    response = requests.post(
        url,
        headers=dhan_headers(access_token),
        json=payload,
        timeout=30
    )

    if response.status_code != 200:
        fail(
            f"Historical data failed for {security_id}: "
            f"{response.status_code} {response.text}"
        )

    data = response.json()

    if "close" not in data:
        fail(
            f"Unexpected historical-data response for "
            f"{security_id}: {data}"
        )

    df = pd.DataFrame({
        "open": data.get("open", []),
        "high": data.get("high", []),
        "low": data.get("low", []),
        "close": data.get("close", []),
        "volume": data.get("volume", []),
        "timestamp": data.get("timestamp", []),
    })

    if df.empty:
        fail(f"No historical data returned for {security_id}.")

    # Dhan timestamps are epoch seconds.
    df["datetime"] = pd.to_datetime(
        df["timestamp"],
        unit="s",
        utc=True
    ).dt.tz_convert(IST)

    return df


# ============================================================
# CALCULATE VWAP
# ============================================================

def calculate_vwap(df):
    """
    Standard candle VWAP using typical price:
    (High + Low + Close) / 3
    """

    if df.empty:
        return None

    df = df.copy()

    df["typical_price"] = (
        df["high"] +
        df["low"] +
        df["close"]
    ) / 3.0

    df["pv"] = (
        df["typical_price"] *
        df["volume"]
    )

    total_volume = df["volume"].sum()

    if total_volume <= 0:
        return None

    return df["pv"].sum() / total_volume


# ============================================================
# GET CURRENT LTP
# ============================================================

def get_ltp(
    access_token,
    exchange_segment,
    security_id
):

    url = f"{DHAN_BASE}/marketfeed/ltp"

    payload = {
        exchange_segment: [int(security_id)]
    }

    response = requests.post(
        url,
        headers=dhan_headers(access_token),
        json=payload,
        timeout=20
    )

    if response.status_code != 200:
        fail(
            f"LTP request failed: "
            f"{response.status_code} {response.text}"
        )

    data = response.json()

    try:
        return float(
            data["data"]
                [exchange_segment]
                [str(security_id)]
                ["last_price"]
        )
    except Exception:
        fail(f"Could not read LTP response: {data}")


# ============================================================
# ABOVE / BELOW
# ============================================================

def above_below(price, vwap):

    if price > vwap:
        return "ABOVE", "🟢"

    if price < vwap:
        return "BELOW", "🔴"

    return "AT VWAP", "🟡"


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message):

    url = (
        f"https://api.telegram.org/bot"
        f"{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
    }

    response = requests.post(
        url,
        json=payload,
        timeout=20
    )

    if response.status_code != 200:
        fail(
            f"Telegram failed: "
            f"{response.status_code} {response.text}"
        )

    print("Telegram message sent successfully.")


# ============================================================
# MAIN
# ============================================================

def main():

    now = datetime.now(IST)

    print("=" * 60)
    print("3:16 PM VWAP ALERT")
    print("=" * 60)
    print(f"Current IST time: {now}")

    # --------------------------------------------------------
    # Safety check
    # --------------------------------------------------------

    if now.weekday() >= 5:
        print("Weekend. No alert.")
        return

    # --------------------------------------------------------
    # Authenticate
    # --------------------------------------------------------

    access_token = generate_access_token()

    # --------------------------------------------------------
    # Instrument master
    # --------------------------------------------------------

    instruments = load_instruments()

    # --------------------------------------------------------
    # VWAP window
    #
    # 3:00:00 PM through 3:14:59 PM
    #
    # 3:15 candle is NOT included.
    # --------------------------------------------------------

    start_time = now.replace(
        hour=15,
        minute=0,
        second=0,
        microsecond=0
    )

    end_time = now.replace(
        hour=15,
        minute=15,
        second=0,
        microsecond=0
    )

    # --------------------------------------------------------
    # NIFTY FUTURE
    # --------------------------------------------------------

    nifty_security = find_current_future(
        instruments,
        "NIFTY"
    )

    nifty_data = get_intraday_data(
        access_token,
        nifty_security,
        "NSE_FNO",
        "FUTIDX",
        start_time,
        end_time
    )

    # Keep only 3:00–3:15 window.
    nifty_data = nifty_data[
        (nifty_data["datetime"] >= start_time) &
        (nifty_data["datetime"] < end_time)
    ]

    nifty_vwap = calculate_vwap(nifty_data)

    if nifty_vwap is None:
        fail("Nifty VWAP could not be calculated.")

    nifty_ltp = get_ltp(
        access_token,
        "NSE_FNO",
        nifty_security
    )

    nifty_status, nifty_icon = above_below(
        nifty_ltp,
        nifty_vwap
    )

    # --------------------------------------------------------
    # SENSEX FUTURE
    # --------------------------------------------------------

    sensex_security = find_current_future(
        instruments,
        "SENSEX"
    )

    sensex_data = get_intraday_data(
        access_token,
        sensex_security,
        "BSE_FNO",
        "FUTIDX",
        start_time,
        end_time
    )

    sensex_data = sensex_data[
        (sensex_data["datetime"] >= start_time) &
        (sensex_data["datetime"] < end_time)
    ]

    sensex_vwap = calculate_vwap(sensex_data)

    if sensex_vwap is None:
        fail("Sensex VWAP could not be calculated.")

    sensex_ltp = get_ltp(
        access_token,
        "BSE_FNO",
        sensex_security
    )

    sensex_status, sensex_icon = above_below(
        sensex_ltp,
        sensex_vwap
    )

    # --------------------------------------------------------
    # TOP 10 NIFTY STOCKS
    # --------------------------------------------------------

    above_count = 0
    below_count = 0
    checked_count = 0

    print("\nTop 10 calculation:")

    for symbol in TOP_10:

        try:
            security_id = find_equity(
                instruments,
                symbol
            )

            stock_data = get_intraday_data(
                access_token,
                security_id,
                "NSE_EQ",
                "EQUITY",
                start_time,
                end_time
            )

            stock_data = stock_data[
                (stock_data["datetime"] >= start_time) &
                (stock_data["datetime"] < end_time)
            ]

            stock_vwap = calculate_vwap(stock_data)

            if stock_vwap is None:
                print(
                    f"{symbol}: VWAP unavailable"
                )
                continue

            stock_ltp = get_ltp(
                access_token,
                "NSE_EQ",
                security_id
            )

            if stock_ltp > stock_vwap:
                above_count += 1
                status = "ABOVE"

            elif stock_ltp < stock_vwap:
                below_count += 1
                status = "BELOW"

            else:
                status = "AT VWAP"

            checked_count += 1

            print(
                f"{symbol}: {status} "
                f"LTP={stock_ltp:.2f} "
                f"VWAP={stock_vwap:.2f}"
            )

            # Dhan quote API has rate limits.
            time.sleep(1.1)

        except Exception as e:
            print(
                f"{symbol}: skipped because of error: {e}"
            )

    if checked_count == 0:
        fail("No Top-10 stocks could be calculated.")

    above_pct = round(
        above_count / checked_count * 100
    )

    below_pct = round(
        below_count / checked_count * 100
    )

    # --------------------------------------------------------
    # CLOSING BIAS
    # --------------------------------------------------------

    bullish_score = 0

    if nifty_status == "ABOVE":
        bullish_score += 1
    elif nifty_status == "BELOW":
        bullish_score -= 1

    if sensex_status == "ABOVE":
        bullish_score += 1
    elif sensex_status == "BELOW":
        bullish_score -= 1

    if above_pct > below_pct:
        bullish_score += 1
    elif below_pct > above_pct:
        bullish_score -= 1

    if bullish_score >= 2:
        bias = "🟢 BULLISH"

    elif bullish_score <= -2:
        bias = "🔴 BEARISH"

    else:
        bias = "🟡 MIXED"

    # --------------------------------------------------------
    # TELEGRAM MESSAGE
    # --------------------------------------------------------

    message = (
        "📊 3:16 PM VWAP\n\n"
        f"Nifty: {nifty_icon} {nifty_status}\n"
        f"Sensex: {sensex_icon} {sensex_status}\n"
        f"Top 10 Nifty Stocks: "
        f"{above_pct}% 🟢 ABOVE | "
        f"{below_pct}% 🔴 BELOW\n\n"
        f"Closing Bias: {bias}"
    )

    print("\n" + message)

    send_telegram(message)


if __name__ == "__main__":
    main()
