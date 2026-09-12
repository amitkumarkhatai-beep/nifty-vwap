import os
import csv
import json
import math
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
import pyotp


# ============================================================
# CAS SIGNAL ENGINE
#
# 14:58  -> start / initialize
# 15:00  -> collection begins
# 15:15  -> exact baseline snapshot selected
# 15:17  -> prediction locked
# 15:30  -> outcome + calibration
#
# NO ORDERS ARE PLACED.
# This system only generates signals and Telegram alerts.
# ============================================================

IST = ZoneInfo("Asia/Kolkata")

API = "https://api.dhan.co/v2"

DATA_DIR = "data"
CALIBRATION_FILE = os.path.join(DATA_DIR, "calibration.jsonl")
STATE_FILE = os.path.join(DATA_DIR, "cas_state.json")

DHAN_CLIENT_ID = os.environ["DHAN_CLIENT_ID"]
DHAN_PIN = os.environ["DHAN_PIN"]
DHAN_TOTP_SECRET = os.environ["DHAN_TOTP_SECRET"]

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ.get(
    "TELEGRAM_CHAT_ID",
    "1001276343"
)

# Optional AI layer.
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get(
    "OPENAI_MODEL",
    "gpt-5.6-luna"
)

# Dhan underlying IDs.
# NIFTY  = 13
# SENSEX = 51 is configurable because the underlying ID
# should be verified against the user's Dhan instrument master.
NIFTY_ID = int(
    os.environ.get("DHAN_NIFTY_ID", "13")
)

SENSEX_ID = int(
    os.environ.get("DHAN_SENSEX_ID", "51")
)

INDEX_SEGMENT = "IDX_I"

# Collection settings.
COLLECTION_START = (15, 0)
PREDICTION_TIME = (15, 17)
BASELINE_TIME = (15, 15)
OUTCOME_TIME = (15, 30)

# Snapshot interval.
# Dhan market quote is rate-limited to 1 request/sec.
# We deliberately use 5 seconds to reduce API pressure.
SNAPSHOT_SECONDS = 5

# Calibration.
MIN_CALIBRATION_SAMPLES = 25

# Meaningful 15:15 -> 15:30 move.
# Below this = effectively noise / mixed.
OUTCOME_THRESHOLD_PCT = 0.08

# Signal threshold.
SIGNAL_THRESHOLD = 0.16


# ============================================================
# HELPERS
# ============================================================

def now_ist():
    return datetime.now(IST)


def sleep_until(target):
    while True:
        seconds = (
            target - now_ist()
        ).total_seconds()

        if seconds <= 0:
            return

        time.sleep(
            min(seconds, 2)
        )


def safe_float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return default


def safe_int(value, default=0):
    try:
        return int(float(value))
    except Exception:
        return default


def clamp(value, low=-1.0, high=1.0):
    return max(
        low,
        min(high, value)
    )


def mean(values):
    values = [
        x for x in values
        if x is not None
    ]

    if not values:
        return 0.0

    return sum(values) / len(values)


def pct_change(old, new):
    if not old or old == 0:
        return 0.0

    return (
        (new - old) / old
    ) * 100.0


def normalize(value, scale):
    if scale == 0:
        return 0.0

    return clamp(
        value / scale
    )


def timestamp():
    return now_ist().isoformat()


def ensure_data_dir():
    os.makedirs(
        DATA_DIR,
        exist_ok=True
    )


# ============================================================
# DHAN AUTHENTICATION
# ============================================================

def dhan_access_token():
    """
    Generate a fresh 24-hour Dhan access token using TOTP.
    """

    totp = pyotp.TOTP(
        DHAN_TOTP_SECRET
    ).now()

    url = (
        "https://auth.dhan.co/app/"
        "generateAccessToken"
        f"?dhanClientId={DHAN_CLIENT_ID}"
        f"&pin={DHAN_PIN}"
        f"&totp={totp}"
    )

    response = requests.post(
        url,
        timeout=20
    )

    response.raise_for_status()

    data = response.json()

    token = data.get(
        "accessToken"
    )

    if not token:
        raise RuntimeError(
            f"Dhan authentication failed: {data}"
        )

    return token


class Dhan:
    def __init__(self):
        self.token = dhan_access_token()

        self.headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "access-token": self.token,
            "client-id": DHAN_CLIENT_ID,
        }

    def post(
        self,
        endpoint,
        payload
    ):
        url = API + endpoint

        response = requests.post(
            url,
            headers=self.headers,
            json=payload,
            timeout=20
        )

        if response.status_code >= 400:
            raise RuntimeError(
                f"Dhan API {endpoint} "
                f"{response.status_code}: "
                f"{response.text[:1000]}"
            )

        return response.json()

    def expiry_list(
        self,
        security_id,
        segment
    ):
        return self.post(
            "/optionchain/expirylist",
            {
                "UnderlyingScrip": security_id,
                "UnderlyingSeg": segment,
            }
        )

    def option_chain(
        self,
        security_id,
        segment,
        expiry
    ):
        return self.post(
            "/optionchain",
            {
                "UnderlyingScrip": security_id,
                "UnderlyingSeg": segment,
                "Expiry": expiry,
            }
        )

    def quote(
        self,
        securities
    ):
        return self.post(
            "/marketfeed/quote",
            securities
        )


# ============================================================
# RESPONSE HELPERS
# ============================================================

def unwrap_data(response):
    if not isinstance(response, dict):
        return {}

    return response.get(
        "data",
        response
    )


def extract_expiries(response):
    data = unwrap_data(response)

    if isinstance(data, list):
        return [
            str(x)
            for x in data
        ]

    if not isinstance(data, dict):
        return []

    for key in (
        "expiry",
        "expiries",
        "Expiry",
        "ExpiryList"
    ):
        value = data.get(key)

        if isinstance(value, list):
            return [
                str(x)
                for x in value
            ]

    return []


def parse_expiry_date(value):
    try:
        return datetime.strptime(
            str(value)[:10],
            "%Y-%m-%d"
        ).date()
    except Exception:
        return None


def first_valid_expiry(
    dhan,
    security_id
):
    response = dhan.expiry_list(
        security_id,
        INDEX_SEGMENT
    )

    expiries = extract_expiries(
        response
    )

    if not expiries:
        raise RuntimeError(
            f"No expiry returned for "
            f"underlying {security_id}"
        )

    today = now_ist().date()

    parsed = []

    for expiry in expiries:
        expiry_date = parse_expiry_date(
            expiry
        )

        if expiry_date:
            parsed.append(
                (
                    expiry_date,
                    expiry
                )
            )

    future = [
        x for x in parsed
        if x[0] >= today
    ]

    if future:
        future.sort(
            key=lambda x: x[0]
        )

        return future[0][1]

    return expiries[0]


# ============================================================
# OPTION CHAIN
# ============================================================

def find_chain_rows(response):
    data = unwrap_data(response)

    if not isinstance(data, dict):
        return {}

    if isinstance(
        data.get("oc"),
        dict
    ):
        return data["oc"]

    return data


def parse_option_rows(response):
    rows = find_chain_rows(
        response
    )

    output = []

    if not isinstance(rows, dict):
        return output

    for strike_key, item in rows.items():

        if not isinstance(item, dict):
            continue

        strike = safe_float(
            item.get(
                "strike_price",
                strike_key
            )
        )

        ce = (
            item.get("ce")
            or item.get("CE")
            or {}
        )

        pe = (
            item.get("pe")
            or item.get("PE")
            or {}
        )

        if isinstance(ce, dict) and ce:
            output.append({
                "strike": strike,
                "side": "CE",
                **ce
            })

        if isinstance(pe, dict) and pe:
            output.append({
                "strike": strike,
                "side": "PE",
                **pe
            })

    return output


def build_atm_contracts(
    dhan,
    security_id,
    label
):
    expiry = first_valid_expiry(
        dhan,
        security_id
    )

    chain = dhan.option_chain(
        security_id,
        INDEX_SEGMENT,
        expiry
    )

    data = unwrap_data(
        chain
    )

    spot = safe_float(
        data.get(
            "last_price",
            0
        )
    )

    rows = parse_option_rows(
        chain
    )

    if not rows:
        raise RuntimeError(
            f"No option-chain rows "
            f"for {label}"
        )

    strikes = sorted(
        set(
            r["strike"]
            for r in rows
            if r.get("strike")
        )
    )

    if not strikes:
        raise RuntimeError(
            f"No strikes available "
            f"for {label}"
        )

    # Dhan provides underlying last_price
    # directly in option-chain response.
    # Only use mean strike as emergency fallback.
    if spot <= 0:
        spot = mean(strikes)

    atm = min(
        strikes,
        key=lambda x:
        abs(x - spot)
    )

    # Find exactly ATM-1, ATM and ATM+1
    # in the sorted strike ladder.
    atm_index = strikes.index(atm)

    nearby_indexes = [
        atm_index - 1,
        atm_index,
        atm_index + 1
    ]

    nearby_indexes = [
        i for i in nearby_indexes
        if 0 <= i < len(strikes)
    ]

    nearby_strikes = [
        strikes[i]
        for i in nearby_indexes
    ]

    contracts = []

    option_segment = (
        "NSE_FNO"
        if label == "NIFTY"
        else "BSE_FNO"
    )

    for strike in nearby_strikes:

        for side in (
            "CE",
            "PE"
        ):

            match = next(
                (
                    r for r in rows
                    if r["strike"] == strike
                    and r["side"] == side
                ),
                None
            )

            if not match:
                continue

            security_id_option = (
                match.get(
                    "security_id"
                )
                or match.get(
                    "SecurityId"
                )
            )

            if not security_id_option:
                continue

            contracts.append({
                "label": label,
                "expiry": expiry,
                "strike": strike,
                "side": side,
                "security_id": str(
                    security_id_option
                ),
                "segment": option_segment,
            })

    if len(contracts) < 6:
        raise RuntimeError(
            f"Could not build full "
            f"ATM ±1 option set for {label}"
        )

    return {
        "label": label,
        "spot": spot,
        "atm": atm,
        "expiry": expiry,
        "contracts": contracts
    }


# ============================================================
# QUOTE NORMALIZATION
# ============================================================

def quote_to_map(response):
    data = unwrap_data(
        response
    )

    result = {}

    if not isinstance(data, dict):
        return result

    for segment, instruments in data.items():

        if not isinstance(
            instruments,
            dict
        ):
            continue

        for security_id, packet in instruments.items():

            if not isinstance(
                packet,
                dict
            ):
                continue

            result[
                str(security_id)
            ] = {
                "segment": segment,
                **packet
            }

    return result


def get_quote(
    dhan,
    instruments
):
    grouped = {}

    for instrument in instruments:

        segment = instrument[
            "segment"
        ]

        security_id = safe_int(
            instrument[
                "security_id"
            ]
        )

        grouped.setdefault(
            segment,
            []
        )

        if security_id not in grouped[
            segment
        ]:
            grouped[
                segment
            ].append(
                security_id
            )

    if not grouped:
        return {}

    response = dhan.quote(
        grouped
    )

    return quote_to_map(
        response
    )


def packet_ltp(packet):
    return safe_float(
        packet.get(
            "last_price"
        )
        or packet.get(
            "ltp"
        )
    )


def packet_volume(packet):
    return safe_float(
        packet.get(
            "volume"
        )
    )


def packet_oi(packet):
    return safe_float(
        packet.get(
            "oi"
        )
    )


def packet_buy_qty(packet):
    return safe_float(
        packet.get(
            "buy_quantity"
        )
        or packet.get(
            "total_buy_quantity"
        )
    )


def packet_sell_qty(packet):
    return safe_float(
        packet.get(
            "sell_quantity"
        )
        or packet.get(
            "total_sell_quantity"
        )
    )


def depth_imbalance(packet):
    buy = packet_buy_qty(
        packet
    )

    sell = packet_sell_qty(
        packet
    )

    total = buy + sell

    if total <= 0:
        return 0.0

    return clamp(
        (buy - sell) / total
    )


# ============================================================
# HEAVYWEIGHTS
# ============================================================

NIFTY_HEAVYWEIGHTS = [
    "RELIANCE",
    "HDFCBANK",
    "ICICIBANK",
    "INFY",
    "TCS",
    "BHARTIARTL",
    "LT",
    "ITC",
    "SBIN",
    "KOTAKBANK",
    "AXISBANK",
    "M&M",
    "HINDUNILVR",
    "BAJFINANCE",
    "SUNPHARMA",
]


def load_instrument_master():
    url = (
        "https://images.dhan.co/api-data/"
        "api-scrip-master-detailed.csv"
    )

    response = requests.get(
        url,
        timeout=30
    )

    response.raise_for_status()

    reader = csv.DictReader(
        response.text.splitlines()
    )

    result = {}

    for row in reader:

        symbol = (
            row.get(
                "SEM_TRADING_SYMBOL"
            )
            or row.get(
                "SEM_CUSTOM_SYMBOL"
            )
            or ""
        ).strip().upper()

        security_id = (
            row.get(
                "SEM_SMST_SECURITY_ID"
            )
            or row.get(
                "security_id"
            )
            or ""
        ).strip()

        exchange = (
            row.get(
                "SEM_EXM_EXCH_ID"
            )
            or ""
        ).strip().upper()

        if (
            symbol
            and security_id
            and exchange == "NSE"
            and symbol in NIFTY_HEAVYWEIGHTS
        ):
            result[symbol] = {
                "security_id": security_id,
                "segment": "NSE_EQ"
            }

    return result


def build_heavyweights():
    try:
        master = load_instrument_master()
    except Exception as exc:
        print(
            "Instrument master error:",
            exc
        )
        return []

    instruments = []

    for symbol in NIFTY_HEAVYWEIGHTS:

        item = master.get(
            symbol
        )

        if not item:
            continue

        instruments.append({
            "symbol": symbol,
            "security_id": item[
                "security_id"
            ],
            "segment": "NSE_EQ"
        })

    return instruments


# ============================================================
# SNAPSHOT
# ============================================================

def make_snapshot(
    quote,
    index_instruments,
    option_contracts,
    heavyweight_instruments
):
    snapshot = {
        "timestamp": timestamp(),
        "indices": {},
        "options": {},
        "heavyweights": {}
    }

    # Index snapshots.
    for instrument in index_instruments:

        sid = str(
            instrument[
                "security_id"
            ]
        )

        packet = quote.get(
            sid,
            {}
        )

        snapshot[
            "indices"
        ][
            instrument["label"]
        ] = {
            "ltp": packet_ltp(
                packet
            ),
            "volume": packet_volume(
                packet
            ),
            "oi": packet_oi(
                packet
            ),
            "imbalance":
                depth_imbalance(
                    packet
                ),
            "buy_qty":
                packet_buy_qty(
                    packet
                ),
            "sell_qty":
                packet_sell_qty(
                    packet
                )
        }

    # Option snapshots.
    for contract in option_contracts:

        sid = str(
            contract[
                "security_id"
            ]
        )

        packet = quote.get(
            sid,
            {}
        )

        key = (
            f'{contract["label"]}_'
            f'{contract["strike"]}_'
            f'{contract["side"]}'
        )

        snapshot[
            "options"
        ][key] = {
            "label":
                contract["label"],
            "strike":
                contract["strike"],
            "side":
                contract["side"],
            "ltp":
                packet_ltp(packet),
            "volume":
                packet_volume(packet),
            "oi":
                packet_oi(packet),
            "imbalance":
                depth_imbalance(packet),
            "buy_qty":
                packet_buy_qty(packet),
            "sell_qty":
                packet_sell_qty(packet)
        }

    # Heavyweights.
    for instrument in heavyweight_instruments:

        sid = str(
            instrument[
                "security_id"
            ]
        )

        packet = quote.get(
            sid,
            {}
        )

        snapshot[
            "heavyweights"
        ][
            instrument["symbol"]
        ] = {
            "ltp":
                packet_ltp(packet),
            "volume":
                packet_volume(packet),
            "imbalance":
                depth_imbalance(packet)
        }

    return snapshot


# ============================================================
# SNAPSHOT MATH
# ============================================================

def snapshot_time(snapshot):
    return datetime.fromisoformat(
        snapshot["timestamp"]
    )


def closest_snapshot(
    snapshots,
    target
):
    if not snapshots:
        return None

    return min(
        snapshots,
        key=lambda s:
        abs(
            (
                snapshot_time(s)
                - target
            ).total_seconds()
        )
    )


def latest_snapshot(
    snapshots
):
    if not snapshots:
        return None

    return max(
        snapshots,
        key=snapshot_time
    )


def delta_volume(
    previous,
    current
):
    if current < previous:
        return 0.0

    return max(
        0.0,
        current - previous
    )


def delta_oi(
    previous,
    current
):
    return current - previous


def option_flow_features(
    first,
    last
):
    result = {}

    labels = (
        "NIFTY",
        "SENSEX"
    )

    for label in labels:

        ce_volume = 0.0
        pe_volume = 0.0

        ce_premium_move = []
        pe_premium_move = []

        ce_oi_change = 0.0
        pe_oi_change = 0.0

        ce_imbalance = []
        pe_imbalance = []

        for key, current in last[
            "options"
        ].items():

            if current["label"] != label:
                continue

            previous = first[
                "options"
            ].get(key)

            if not previous:
                continue

            dv = delta_volume(
                previous["volume"],
                current["volume"]
            )

            doi = delta_oi(
                previous["oi"],
                current["oi"]
            )

            premium = pct_change(
                previous["ltp"],
                current["ltp"]
            )

            if current["side"] == "CE":

                ce_volume += dv
                ce_oi_change += doi

                ce_premium_move.append(
                    premium
                )

                ce_imbalance.append(
                    current["imbalance"]
                )

            else:

                pe_volume += dv
                pe_oi_change += doi

                pe_premium_move.append(
                    premium
                )

                pe_imbalance.append(
                    current["imbalance"]
                )

        total_volume = (
            ce_volume
            + pe_volume
        )

        if total_volume > 0:
            volume_balance = (
                ce_volume
                - pe_volume
            ) / total_volume
        else:
            volume_balance = 0.0

        premium_balance = clamp(
            (
                mean(ce_premium_move)
                - mean(pe_premium_move)
            ) / 2.0
        )

        oi_total = (
            abs(ce_oi_change)
            + abs(pe_oi_change)
        )

        if oi_total > 0:
            # More PE OI relative to CE OI
            # is treated as bullish evidence.
            oi_balance = (
                pe_oi_change
                - ce_oi_change
            ) / oi_total
        else:
            oi_balance = 0.0

        bid_ask_balance = clamp(
            (
                mean(ce_imbalance)
                - mean(pe_imbalance)
            )
        )

        result[label] = {
            "ce_volume":
                ce_volume,
            "pe_volume":
                pe_volume,
            "ce_pe_ratio":
                (
                    ce_volume / pe_volume
                    if pe_volume > 0
                    else (
                        999.0
                        if ce_volume > 0
                        else 1.0
                    )
                ),
            "volume_balance":
                volume_balance,
            "premium_balance":
                premium_balance,
            "ce_oi_change":
                ce_oi_change,
            "pe_oi_change":
                pe_oi_change,
            "oi_balance":
                oi_balance,
            "bid_ask_balance":
                bid_ask_balance
        }

    return result


# ============================================================
# INDEX FEATURES
# ============================================================

def index_features(
    first,
    last
):
    result = {}

    for label in (
        "NIFTY",
        "SENSEX"
    ):

        a = first[
            "indices"
        ].get(label, {})

        b = last[
            "indices"
        ].get(label, {})

        first_ltp = a.get(
            "ltp",
            0
        )

        last_ltp = b.get(
            "ltp",
            0
        )

        return_pct = pct_change(
            first_ltp,
            last_ltp
        )

        imbalance = b.get(
            "imbalance",
            0
        )

        imbalance_change = (
            imbalance
            - a.get(
                "imbalance",
                0
            )
        )

        volume_change = delta_volume(
            a.get("volume", 0),
            b.get("volume", 0)
        )

        result[label] = {
            "first_ltp":
                first_ltp,
            "last_ltp":
                last_ltp,
            "return_pct":
                return_pct,
            "imbalance":
                imbalance,
            "imbalance_change":
                imbalance_change,
            "volume_change":
                volume_change
        }

    return result


# ============================================================
# HEAVYWEIGHT FEATURES
# ============================================================

def heavyweight_features(
    first,
    last
):
    confirmations = []

    total_score = 0.0

    for symbol, current in last[
        "heavyweights"
    ].items():

        previous = first[
            "heavyweights"
        ].get(symbol)

        if not previous:
            continue

        old_ltp = previous.get(
            "ltp",
            0
        )

        new_ltp = current.get(
            "ltp",
            0
        )

        move = pct_change(
            old_ltp,
            new_ltp
        )

        imbalance = current.get(
            "imbalance",
            0
        )

        # Price gets the highest influence.
        score = (
            clamp(
                move / 0.20
            ) * 0.60
            + imbalance * 0.40
        )

        total_score += score

        if score > 0.10:
            confirmations.append(
                1
            )
        elif score < -0.10:
            confirmations.append(
                -1
            )
        else:
            confirmations.append(
                0
            )

    if confirmations:
        positive = sum(
            1 for x in confirmations
            if x == 1
        )

        negative = sum(
            1 for x in confirmations
            if x == -1
        )

        usable = (
            positive
            + negative
        )

        breadth = (
            (positive - negative)
            / usable
            if usable
            else 0.0
        )

        average_score = (
            total_score
            / len(confirmations)
        )
    else:
        positive = 0
        negative = 0
        breadth = 0.0
        average_score = 0.0

    return {
        "positive":
            positive,
        "negative":
            negative,
        "breadth":
            breadth,
        "average_score":
            clamp(
                average_score
            ),
        "count":
            len(confirmations)
    }


# ============================================================
# WINDOW STRUCTURE / VWAP PROXY
# ============================================================

def window_features(
    snapshots,
    label
):
    if not snapshots:
        return {
            "high": 0.0,
            "low": 0.0,
            "return_pct": 0.0,
            "vwap": 0.0,
            "last_price": 0.0,
            "momentum": 0.0
        }

    prices = []

    for snapshot in snapshots:
        item = snapshot[
            "indices"
        ].get(label)

        if item and item["ltp"] > 0:
            prices.append(
                (
                    snapshot_time(
                        snapshot
                    ),
                    item["ltp"],
                    item["volume"]
                )
            )

    if not prices:
        return {
            "high": 0.0,
            "low": 0.0,
            "return_pct": 0.0,
            "vwap": 0.0,
            "last_price": 0.0,
            "momentum": 0.0
        }

    high = max(
        x[1] for x in prices
    )

    low = min(
        x[1] for x in prices
    )

    first_price = prices[0][1]
    last_price = prices[-1][1]

    # Volume-weighted proxy.
    # Market Quote volume is cumulative day volume.
    total_volume = 0.0
    weighted = 0.0

    previous_volume = prices[0][2]

    for _, price, volume in prices:

        incremental = max(
            0.0,
            volume - previous_volume
        )

        if incremental > 0:
            weighted += (
                price
                * incremental
            )

            total_volume += incremental

        previous_volume = volume

    if total_volume > 0:
        vwap = (
            weighted
            / total_volume
        )
    else:
        vwap = mean(
            [x[1] for x in prices]
        )

    # Last 3 samples = short momentum.
    recent = prices[-3:]

    if len(recent) >= 2:
        momentum = pct_change(
            recent[0][1],
            recent[-1][1]
        )
    else:
        momentum = 0.0

    return {
        "high": high,
        "low": low,
        "return_pct": pct_change(
            first_price,
            last_price
        ),
        "vwap": vwap,
        "last_price": last_price,
        "momentum": momentum
    }


# ============================================================
# REGIME
# ============================================================

def detect_regime(
    snapshots,
    label
):
    wf = window_features(
        snapshots,
        label
    )

    prices = []

    for snapshot in snapshots:
        item = snapshot[
            "indices"
        ].get(label)

        if item:
            if item["ltp"] > 0:
                prices.append(
                    item["ltp"]
                )

    if len(prices) < 3:
        return {
            "regime": "UNKNOWN",
            "volatility": 0.0
        }

    returns = []

    for i in range(
        1,
        len(prices)
    ):
        if prices[i - 1] != 0:
            returns.append(
                (
                    prices[i]
                    - prices[i - 1]
                )
                / prices[i - 1]
                * 100
            )

    if returns:
        volatility = math.sqrt(
            mean(
                [
                    x * x
                    for x in returns
                ]
            )
        )
    else:
        volatility = 0.0

    range_pct = (
        pct_change(
            wf["low"],
            wf["high"]
        )
        if wf["low"] > 0
        else 0
    )

    if volatility > 0.12:
        regime = "HIGH_VOLATILITY"
    elif abs(wf["return_pct"]) > 0.35:
        regime = "TREND"
    elif range_pct < 0.25:
        regime = "RANGE"
    else:
        regime = "NORMAL"

    return {
        "regime": regime,
        "volatility": volatility
    }


# ============================================================
# EXPIRY PRIORITY
# ============================================================

def is_expiry_day(expiry):
    expiry_date = parse_expiry_date(
        expiry
    )

    if not expiry_date:
        return False

    return (
        expiry_date
        == now_ist().date()
    )


def determine_priority(
    nifty_expiry,
    sensex_expiry
):
    nifty_expiry_day = is_expiry_day(
        nifty_expiry
    )

    sensex_expiry_day = is_expiry_day(
        sensex_expiry
    )

    if nifty_expiry_day:
        return "NIFTY"

    if sensex_expiry_day:
        return "SENSEX"

    return "NIFTY"


# ============================================================
# FEATURE ENGINE
# ============================================================

def build_features(
    snapshots,
    baseline
):
    latest = latest_snapshot(
        snapshots
    )

    if not latest or not baseline:
        raise RuntimeError(
            "Insufficient snapshots"
        )

    # Use all samples from collection.
    option_features = option_flow_features(
        snapshots[0],
        latest
    )

    idx_features = index_features(
        snapshots[0],
        latest
    )

    hw_features = heavyweight_features(
        snapshots[0],
        latest
    )

    window = {
        "NIFTY":
            window_features(
                snapshots,
                "NIFTY"
            ),
        "SENSEX":
            window_features(
                snapshots,
                "SENSEX"
            )
    }

    regime = {
        "NIFTY":
            detect_regime(
                snapshots,
                "NIFTY"
            ),
        "SENSEX":
            detect_regime(
                snapshots,
                "SENSEX"
            )
    }

    baseline_features = {}

    for label in (
        "NIFTY",
        "SENSEX"
    ):

        base = baseline[
            "indices"
        ].get(label, {})

        current = latest[
            "indices"
        ].get(label, {})

        baseline_features[label] = {
            "baseline_price":
                base.get(
                    "ltp",
                    0
                ),
            "latest_price":
                current.get(
                    "ltp",
                    0
                ),
            "baseline_to_latest":
                pct_change(
                    base.get(
                        "ltp",
                        0
                    ),
                    current.get(
                        "ltp",
                        0
                    )
                )
        }

    return {
        "options":
            option_features,
        "indices":
            idx_features,
        "heavyweights":
            hw_features,
        "window":
            window,
        "regime":
            regime,
        "baseline":
            baseline_features,
        "sample_count":
            len(snapshots),
        "generated_at":
            timestamp()
    }


# ============================================================
# CALIBRATION
# ============================================================

DEFAULT_WEIGHTS = {
    "option_volume": 0.24,
    "option_premium": 0.15,
    "option_oi": 0.12,
    "option_imbalance": 0.08,
    "index_return": 0.14,
    "index_imbalance": 0.10,
    "heavyweights": 0.12,
    "momentum": 0.05
}


def load_calibration():
    ensure_data_dir()

    if not os.path.exists(
        CALIBRATION_FILE
    ):
        return []

    rows = []

    try:
        with open(
            CALIBRATION_FILE,
            "r",
            encoding="utf-8"
        ) as file:

            for line in file:

                line = line.strip()

                if not line:
                    continue

                try:
                    rows.append(
                        json.loads(line)
                    )
                except Exception:
                    continue

    except Exception as exc:
        print(
            "Calibration read error:",
            exc
        )

    return rows


def save_calibration(record):
    ensure_data_dir()

    with open(
        CALIBRATION_FILE,
        "a",
        encoding="utf-8"
    ) as file:

        file.write(
            json.dumps(
                record,
                separators=(
                    ",",
                    ":"
                ),
                default=str
            )
            + "\n"
        )


def historical_direction_accuracy(
    rows
):
    usable = [
        x for x in rows
        if x.get("correct") is not None
    ]

    if not usable:
        return None

    correct = sum(
        1 for x in usable
        if x["correct"]
    )

    return (
        correct
        / len(usable)
    )


def calibrated_weights():
    """
    Walk-forward calibration.

    Important:
    We do NOT allow the calibration system to
    completely rewrite the model after a few trades.

    Minimum sample:
        25 evaluated sessions.

    Adjustments are bounded.
    """

    rows = load_calibration()

    usable = [
        x for x in rows
        if x.get("features")
        and x.get("correct") is not None
    ]

    if len(usable) < MIN_CALIBRATION_SAMPLES:
        return dict(
            DEFAULT_WEIGHTS
        )

    weights = dict(
        DEFAULT_WEIGHTS
    )

    # Recent sample gets slightly more importance.
    recent = usable[-100:]

    scores = {
        key: []
        for key in weights
    }

    for row in recent:

        features = row.get(
            "features",
            {}
        )

        outcome = row.get(
            "outcome_direction"
        )

        if outcome not in (
            "BULLISH",
            "BEARISH"
        ):
            continue

        target = (
            1
            if outcome == "BULLISH"
            else -1
        )

        for key in scores:

            value = safe_float(
                features.get(
                    key,
                    0
                )
            )

            scores[key].append(
                value * target
            )

    for key, values in scores.items():

        if not values:
            continue

        edge = mean(values)

        # Small, bounded adjustment.
        adjustment = clamp(
            edge * 0.15,
            -0.035,
            0.035
        )

        weights[key] = clamp(
            weights[key]
            + adjustment,
            0.03,
            0.35
        )

    total = sum(
        weights.values()
    )

    if total <= 0:
        return dict(
            DEFAULT_WEIGHTS
        )

    for key in weights:
        weights[key] /= total

    return weights


# ============================================================
# SIGNAL SCORING
# ============================================================

def score_index(
    label,
    features,
    weights
):
    options = features[
        "options"
    ][label]

    index = features[
        "indices"
    ][label]

    window = features[
        "window"
    ][label]

    hw = features[
        "heavyweights"
    ]

    # -------------------------
    # Option volume
    # -------------------------
    option_volume = clamp(
        options[
            "volume_balance"
        ]
    )

    # -------------------------
    # Premium movement
    # CE rising vs PE rising
    # -------------------------
    option_premium = clamp(
        options[
            "premium_balance"
        ]
    )

    # -------------------------
    # OI
    # -------------------------
    option_oi = clamp(
        options[
            "oi_balance"
        ]
    )

    # -------------------------
    # Option bid/ask
    # -------------------------
    option_imbalance = clamp(
        options[
            "bid_ask_balance"
        ]
    )

    # -------------------------
    # Index price
    # -------------------------
    index_return = clamp(
        index[
            "return_pct"
        ] / 0.40
    )

    # -------------------------
    # Index bid/ask
    # -------------------------
    index_imbalance = clamp(
        index[
            "imbalance"
        ]
    )

    # -------------------------
    # Heavyweight confirmation
    # -------------------------
    heavyweight_score = clamp(
        hw[
            "average_score"
        ]
    )

    # -------------------------
    # Momentum
    # -------------------------
    momentum = clamp(
        window[
            "momentum"
        ] / 0.20
    )

    components = {
        "option_volume":
            option_volume,
        "option_premium":
            option_premium,
        "option_oi":
            option_oi,
        "option_imbalance":
            option_imbalance,
        "index_return":
            index_return,
        "index_imbalance":
            index_imbalance,
        "heavyweights":
            heavyweight_score,
        "momentum":
            momentum
    }

    score = sum(
        components[key]
        * weights[key]
        for key in components
    )

    # VWAP structure.
    vwap = window[
        "vwap"
    ]

    last_price = window[
        "last_price"
    ]

    if vwap > 0 and last_price > 0:

        vwap_signal = clamp(
            (
                last_price
                - vwap
            )
            / (
                vwap
                * 0.0015
            )
        )

        score += (
            vwap_signal
            * 0.06
        )

    score = clamp(
        score
    )

    return {
        "score":
            score,
        "components":
            components
    }


def direction_from_score(
    score
):
    if score >= SIGNAL_THRESHOLD:
        return "BULLISH"

    if score <= -SIGNAL_THRESHOLD:
        return "BEARISH"

    return "NO CLEAR SIGNAL"


def confidence_from_score(
    score,
    sample_count
):
    absolute = abs(
        score
    )

    # Confidence is intentionally capped.
    # More data increases reliability,
    # but never creates artificial certainty.
    base = (
        absolute
        * 100
    )

    if sample_count < 5:
        base *= 0.75

    return int(
        max(
            50,
            min(
                95,
                base
            )
        )
    )


# ============================================================
# AI LAYER
# ============================================================

def ai_classification(
    features,
    preliminary
):
    """
    Optional AI confirmation.

    AI does NOT get to rewrite quantitative
    features or weights.

    It only classifies the existing evidence.
    """

    if not OPENAI_API_KEY:
        return {
            "enabled": False,
            "direction":
                preliminary[
                    "direction"
                ],
            "confidence":
                preliminary[
                    "confidence"
                ],
            "reason":
                "AI disabled"
        }

    payload_features = {
        "nifty": {
            "score":
                preliminary[
                    "nifty_score"
                ],
            "options":
                features[
                    "options"
                ]["NIFTY"],
            "index":
                features[
                    "indices"
                ]["NIFTY"],
            "regime":
                features[
                    "regime"
                ]["NIFTY"]
        },
        "sensex": {
            "score":
                preliminary[
                    "sensex_score"
                ],
            "options":
                features[
                    "options"
                ]["SENSEX"],
            "index":
                features[
                    "indices"
                ]["SENSEX"],
            "regime":
                features[
                    "regime"
                ]["SENSEX"]
        }
    }

    prompt = f"""
You are a market-signal validation layer.

Do not invent data.
Do not place trades.
Do not change quantitative weights.

Review these already-calculated CAS features.

Return JSON only:

{{
  "direction": "BULLISH|BEARISH|NO CLEAR SIGNAL",
  "confidence": 0-100,
  "reason": "short explanation"
}}

If evidence conflicts, choose NO CLEAR SIGNAL.

DATA:
{json.dumps(payload_features, default=str)}
"""

    try:
        response = requests.post(
            "https://api.openai.com/v1/responses",
            headers={
                "Authorization":
                    f"Bearer {OPENAI_API_KEY}",
                "Content-Type":
                    "application/json"
            },
            json={
                "model":
                    OPENAI_MODEL,
                "input":
                    prompt
            },
            timeout=30
        )

        response.raise_for_status()

        data = response.json()

        text = ""

        if isinstance(
            data.get("output_text"),
            str
        ):
            text = data[
                "output_text"
            ]

        if not text:
            # Fallback parser.
            output = data.get(
                "output",
                []
            )

            for item in output:
                for content in item.get(
                    "content",
                    []
                ):
                    if (
                        content.get(
                            "type"
                        )
                        == "output_text"
                    ):
                        text += content.get(
                            "text",
                            ""
                        )

        text = text.strip()

        # Remove accidental markdown fences.
        text = (
            text.replace(
                "```json",
                ""
            )
            .replace(
                "```",
                ""
            )
            .strip()
        )

        parsed = json.loads(
            text
        )

        direction = parsed.get(
            "direction",
            "NO CLEAR SIGNAL"
        )

        confidence = safe_int(
            parsed.get(
                "confidence",
                50
            ),
            50
        )

        if direction not in (
            "BULLISH",
            "BEARISH",
            "NO CLEAR SIGNAL"
        ):
            direction = (
                "NO CLEAR SIGNAL"
            )

        return {
            "enabled": True,
            "direction":
                direction,
            "confidence":
                max(
                    0,
                    min(
                        100,
                        confidence
                    )
                ),
            "reason":
                str(
                    parsed.get(
                        "reason",
                        ""
                    )
                )[:500]
        }

    except Exception as exc:
        print(
            "AI layer error:",
            exc
        )

        return {
            "enabled": True,
            "direction":
                preliminary[
                    "direction"
                ],
            "confidence":
                preliminary[
                    "confidence"
                ],
            "reason":
                "AI unavailable"
        }


# ============================================================
# FINAL SIGNAL
# ============================================================

def generate_signal(
    features,
    priority
):
    weights = calibrated_weights()

    nifty = score_index(
        "NIFTY",
        features,
        weights
    )

    sensex = score_index(
        "SENSEX",
        features,
        weights
    )

    nifty_direction = (
        direction_from_score(
            nifty["score"]
        )
    )

    sensex_direction = (
        direction_from_score(
            sensex["score"]
        )
    )

    # Priority gets a modest additional weight,
    # not an arbitrary override.
    if priority == "NIFTY":
        final_score = (
            nifty["score"] * 0.65
            + sensex["score"] * 0.35
        )
    else:
        final_score = (
            nifty["score"] * 0.35
            + sensex["score"] * 0.65
        )

    direction = (
        direction_from_score(
            final_score
        )
    )

    confidence = (
        confidence_from_score(
            final_score,
            features[
                "sample_count"
            ]
        )
    )

    preliminary = {
        "direction":
            direction,
        "confidence":
            confidence,
        "nifty_score":
            nifty["score"],
        "sensex_score":
            sensex["score"]
    }

    ai = ai_classification(
        features,
        preliminary
    )

    # AI can veto a weak/conflicted signal,
    # but it cannot turn a strong quantitative
    # signal into an opposite trade direction.
    if ai["enabled"]:

        if (
            direction
            == "NO CLEAR SIGNAL"
        ):
            final_direction = (
                "NO CLEAR SIGNAL"
            )

        elif (
            ai["direction"]
            == "NO CLEAR SIGNAL"
        ):
            final_direction = (
                "NO CLEAR SIGNAL"
            )

        elif (
            ai["direction"]
            == direction
        ):
            final_direction = direction

        else:
            # Quant + AI disagreement.
            final_direction = (
                "NO CLEAR SIGNAL"
            )

        final_confidence = min(
            confidence,
            ai["confidence"]
        )

        if (
            final_direction
            == "NO CLEAR SIGNAL"
        ):
            final_confidence = min(
                final_confidence,
                60
            )

    else:
        final_direction = direction
        final_confidence = confidence

    return {
        "priority":
            priority,
        "direction":
            final_direction,
        "confidence":
            final_confidence,
        "nifty_direction":
            nifty_direction,
        "nifty_score":
            nifty["score"],
        "sensex_direction":
            sensex_direction,
        "sensex_score":
            sensex["score"],
        "weights":
            weights,
        "ai":
            ai
    }


# ============================================================
# TELEGRAM
# ============================================================

def telegram_send(message):
    url = (
        "https://api.telegram.org/bot"
        f"{TELEGRAM_BOT_TOKEN}"
        "/sendMessage"
    )

    response = requests.post(
        url,
        json={
            "chat_id":
                TELEGRAM_CHAT_ID,
            "text":
                message
        },
        timeout=20
    )

    if response.status_code >= 400:
        raise RuntimeError(
            "Telegram error: "
            + response.text[:1000]
        )


def emoji_for_direction(
    direction
):
    if direction == "BULLISH":
        return "🟢"

    if direction == "BEARISH":
        return "🔴"

    return "🟡"


# ============================================================
# TELEGRAM MESSAGE
# ============================================================

def format_signal_message(
    signal,
    features,
    expiry_info
):
    priority = signal[
        "priority"
    ]

    direction = signal[
        "direction"
    ]

    confidence = signal[
        "confidence"
    ]

    nifty_opt = features[
        "options"
    ]["NIFTY"]

    sensex_opt = features[
        "options"
    ]["SENSEX"]

    nifty_idx = features[
        "indices"
    ]["NIFTY"]

    sensex_idx = features[
        "indices"
    ]["SENSEX"]

    hw = features[
        "heavyweights"
    ]

    nifty_emoji = emoji_for_direction(
        signal[
            "nifty_direction"
        ]
    )

    sensex_emoji = emoji_for_direction(
        signal[
            "sensex_direction"
        ]
    )

    final_emoji = emoji_for_direction(
        direction
    )

    ai = signal[
        "ai"
    ]

    if ai["enabled"]:
        ai_text = (
            f'AI: {ai["direction"]} '
            f'({ai["confidence"]}%)'
        )
    else:
        ai_text = "AI: OFF"

    nifty_flow = (
        "CE"
        if nifty_opt[
            "volume_balance"
        ] > 0.10
        else (
            "PE"
            if nifty_opt[
                "volume_balance"
            ] < -0.10
            else "MIXED"
        )
    )

    sensex_flow = (
        "CE"
        if sensex_opt[
            "volume_balance"
        ] > 0.10
        else (
            "PE"
            if sensex_opt[
                "volume_balance"
            ] < -0.10
            else "MIXED"
        )
    )

    x_post = (
        f"{final_emoji} CAS SIGNAL | 3:17 PM\n"
        f"{priority} PRIORITY | "
        f"{direction} | "
        f"{confidence}% confidence\n\n"
        f"NIFTY: "
        f"{nifty_emoji} "
        f"{signal['nifty_direction']} "
        f"({signal['nifty_score']:.2f})\n"
        f"ATM±1 Flow: {nifty_flow}\n"
        f"Index: "
        f"{nifty_idx['return_pct']:+.2f}%\n\n"
        f"SENSEX: "
        f"{sensex_emoji} "
        f"{signal['sensex_direction']} "
        f"({signal['sensex_score']:.2f})\n"
        f"ATM±1 Flow: {sensex_flow}\n"
        f"Index: "
        f"{sensex_idx['return_pct']:+.2f}%\n\n"
        f"Heavyweights: "
        f"{hw['positive']} bullish / "
        f"{hw['negative']} bearish\n"
        f"{ai_text}\n\n"
        f"#Nifty #Sensex #CAS "
        f"#OptionsTrading #OrderFlow "
        f"#IndianStockMarket"
    )

    return x_post


# ============================================================
# OUTCOME
# ============================================================

def calculate_outcome(
    baseline,
    outcome_snapshot,
    priority
):
    result = {}

    for label in (
        "NIFTY",
        "SENSEX"
    ):

        base = baseline[
            "indices"
        ].get(label, {})

        outcome = outcome_snapshot[
            "indices"
        ].get(label, {})

        base_price = base.get(
            "ltp",
            0
        )

        outcome_price = outcome.get(
            "ltp",
            0
        )

        move = pct_change(
            base_price,
            outcome_price
        )

        if move > OUTCOME_THRESHOLD_PCT:
            direction = "BULLISH"
        elif move < -OUTCOME_THRESHOLD_PCT:
            direction = "BEARISH"
        else:
            direction = "MIXED"

        result[label] = {
            "baseline":
                base_price,
            "outcome":
                outcome_price,
            "move_pct":
                move,
            "direction":
                direction
        }

    priority_outcome = result[
        priority
    ]

    return {
        "indices":
            result,
        "priority":
            priority,
        "direction":
            priority_outcome[
                "direction"
            ],
        "move_pct":
            priority_outcome[
                "move_pct"
            ]
    }


def evaluate_prediction(
    prediction,
    outcome
):
    predicted = prediction[
        "direction"
    ]

    actual = outcome[
        "direction"
    ]

    if predicted == "NO CLEAR SIGNAL":
        return {
            "correct":
                None,
            "evaluation":
                "NO_SIGNAL"
        }

    if actual == "MIXED":
        return {
            "correct":
                None,
            "evaluation":
                "MIXED"
        }

    return {
        "correct":
            predicted == actual,
        "evaluation":
            (
                "CORRECT"
                if predicted == actual
                else "WRONG"
            )
    }


# ============================================================
# OUTCOME TELEGRAM
# ============================================================

def format_outcome_message(
    prediction,
    outcome,
    evaluation
):
    direction = prediction[
        "direction"
    ]

    actual = outcome[
        "direction"
    ]

    emoji = (
        "✅"
        if evaluation[
            "evaluation"
        ] == "CORRECT"
        else (
            "❌"
            if evaluation[
                "evaluation"
            ] == "WRONG"
            else "⚪"
        )
    )

    move = outcome[
        "move_pct"
    ]

    return (
        f"{emoji} CAS OUTCOME | 3:30 PM\n\n"
        f"Prediction: "
        f"{direction}\n"
        f"Actual: "
        f"{actual}\n"
        f"Move: "
        f"{move:+.2f}%\n"
        f"Result: "
        f"{evaluation['evaluation']}\n\n"
        f"Baseline: 3:15 PM\n"
        f"Outcome: 3:30 PM\n\n"
        f"#Nifty #Sensex #CAS "
        f"#Trading #Calibration"
    )


# ============================================================
# MAIN COLLECTION
# ============================================================

def collect_session(
    dhan,
    index_instruments,
    option_contracts,
    heavyweight_instruments
):
    today = now_ist().date()

    start = datetime(
        today.year,
        today.month,
        today.day,
        COLLECTION_START[0],
        COLLECTION_START[1],
        tzinfo=IST
    )

    prediction_time = datetime(
        today.year,
        today.month,
        today.day,
        PREDICTION_TIME[0],
        PREDICTION_TIME[1],
        tzinfo=IST
    )

    if now_ist() < start:
        sleep_until(
            start
        )

    snapshots = []

    while now_ist() < prediction_time:

        try:
            instruments = (
                index_instruments
                + option_contracts
                + heavyweight_instruments
            )

            quote = get_quote(
                dhan,
                instruments
            )

            snapshot = make_snapshot(
                quote,
                index_instruments,
                option_contracts,
                heavyweight_instruments
            )

            snapshots.append(
                snapshot
            )

            print(
                "Snapshot:",
                snapshot[
                    "timestamp"
                ],
                "count:",
                len(snapshots)
            )

        except Exception as exc:
            print(
                "Snapshot error:",
                exc
            )

        remaining = (
            prediction_time
            - now_ist()
        ).total_seconds()

        if remaining <= 0:
            break

        time.sleep(
            min(
                SNAPSHOT_SECONDS,
                max(
                    1,
                    remaining
                )
            )
        )

    return snapshots


# ============================================================
# 3:30 OUTCOME COLLECTION
# ============================================================

def collect_outcome(
    dhan,
    index_instruments
):
    today = now_ist().date()

    target = datetime(
        today.year,
        today.month,
        today.day,
        OUTCOME_TIME[0],
        OUTCOME_TIME[1],
        tzinfo=IST
    )

    if now_ist() < target:
        sleep_until(
            target
        )

    quote = get_quote(
        dhan,
        index_instruments
    )

    return make_snapshot(
        quote,
        index_instruments,
        [],
        []
    )


# ============================================================
# SAVE COMPLETE SESSION
# ============================================================

def save_state(state):
    ensure_data_dir()

    with open(
        STATE_FILE,
        "w",
        encoding="utf-8"
    ) as file:

        json.dump(
            state,
            file,
            indent=2,
            default=str
        )


# ============================================================
# MAIN
# ============================================================

def main():

    print(
        "=" * 60
    )

    print(
        "CAS ENGINE START"
    )

    print(
        "Time:",
        timestamp()
    )

    print(
        "=" * 60
    )

    ensure_data_dir()

    dhan = Dhan()

    # ----------------------------------------
    # Build underlying instruments.
    # ----------------------------------------

    index_instruments = [
        {
            "label":
                "NIFTY",
            "security_id":
                str(NIFTY_ID),
            "segment":
                INDEX_SEGMENT
        },
        {
            "label":
                "SENSEX",
            "security_id":
                str(SENSEX_ID),
            "segment":
                INDEX_SEGMENT
        }
    ]

    # ----------------------------------------
    # Build ATM ±1 options.
    # ----------------------------------------

    nifty_options = build_atm_contracts(
        dhan,
        NIFTY_ID,
        "NIFTY"
    )

    sensex_options = build_atm_contracts(
        dhan,
        SENSEX_ID,
        "SENSEX"
    )

    option_contracts = (
        nifty_options["contracts"]
        + sensex_options["contracts"]
    )

    print(
        "NIFTY ATM:",
        nifty_options["atm"],
        "Expiry:",
        nifty_options["expiry"]
    )

    print(
        "SENSEX ATM:",
        sensex_options["atm"],
        "Expiry:",
        sensex_options["expiry"]
    )

    # ----------------------------------------
    # Heavyweights.
    # ----------------------------------------

    heavyweight_instruments = (
        build_heavyweights()
    )

    print(
        "Heavyweights loaded:",
        len(
            heavyweight_instruments
        )
    )

    # ----------------------------------------
    # Priority.
    # ----------------------------------------

    priority = determine_priority(
        nifty_options["expiry"],
        sensex_options["expiry"]
    )

    print(
        "Priority:",
        priority
    )

    # ----------------------------------------
    # 3:00 -> 3:17 collection.
    # ----------------------------------------

    snapshots = collect_session(
        dhan,
        index_instruments,
        option_contracts,
        heavyweight_instruments
    )

    if len(snapshots) < 3:
        raise RuntimeError(
            "Not enough market snapshots "
            "to generate a reliable signal."
        )

    # ----------------------------------------
    # IMPORTANT:
    # Exact 3:15 baseline.
    #
    # We DO NOT query the market here.
    # We select the closest snapshot
    # that was already captured during
    # the 3:00-3:17 collection.
    # ----------------------------------------

    today = now_ist().date()

    baseline_target = datetime(
        today.year,
        today.month,
        today.day,
        BASELINE_TIME[0],
        BASELINE_TIME[1],
        tzinfo=IST
    )

    baseline = closest_snapshot(
        snapshots,
        baseline_target
    )

    baseline_age = abs(
        (
            snapshot_time(
                baseline
            )
            - baseline_target
        ).total_seconds()
    )

    if baseline_age > 45:
        raise RuntimeError(
            "No reliable 3:15 PM baseline. "
            f"Closest snapshot is "
            f"{baseline_age:.0f}s away."
        )

    print(
        "3:15 baseline:",
        baseline["timestamp"],
        f"({baseline_age:.1f}s away)"
    )

    # ----------------------------------------
    # Build features using ALL data through
    # 3:17.
    # ----------------------------------------

    features = build_features(
        snapshots,
        baseline
    )

    # ----------------------------------------
    # Generate signal.
    # ----------------------------------------

    signal = generate_signal(
        features,
        priority
    )

    print(
        "NIFTY:",
        signal["nifty_direction"],
        signal["nifty_score"]
    )

    print(
        "SENSEX:",
        signal["sensex_direction"],
        signal["sensex_score"]
    )

    print(
        "FINAL:",
        signal["direction"],
        signal["confidence"]
    )

    # ----------------------------------------
    # Telegram.
    # ----------------------------------------

    signal_message = format_signal_message(
        signal,
        features,
        {
            "nifty":
                nifty_options["expiry"],
            "sensex":
                sensex_options["expiry"]
        }
    )

    try:
        telegram_send(
            signal_message
        )
    except Exception as exc:
        print(
            "Telegram signal error:",
            exc
        )

    # ----------------------------------------
    # Save state so that the complete
    # 3:17 prediction is preserved.
    # ----------------------------------------

    state = {
        "date":
            str(today),
        "prediction_time":
            timestamp(),
        "baseline":
            baseline,
        "features":
            features,
        "prediction":
            signal,
        "priority":
            priority,
        "snapshots":
            snapshots
    }

    save_state(
        state
    )

    # ----------------------------------------
    # 3:17 -> 3:30.
    # ----------------------------------------

    outcome_snapshot = collect_outcome(
        dhan,
        index_instruments
    )

    # ----------------------------------------
    # 3:15 -> 3:30 outcome.
    # ----------------------------------------

    outcome = calculate_outcome(
        baseline,
        outcome_snapshot,
        priority
    )

    evaluation = evaluate_prediction(
        signal,
        outcome
    )

    print(
        "OUTCOME:",
        outcome
    )

    print(
        "EVALUATION:",
        evaluation
    )

    # ----------------------------------------
    # Calibration record.
    # ----------------------------------------

    calibration_features = {}

    # Preserve the actual quantitative
    # feature values used by the model.
    calibration_features[
        "option_volume"
    ] = mean([
        features["options"]["NIFTY"][
            "volume_balance"
        ],
        features["options"]["SENSEX"][
            "volume_balance"
        ]
    ])

    calibration_features[
        "option_premium"
    ] = mean([
        features["options"]["NIFTY"][
            "premium_balance"
        ],
        features["options"]["SENSEX"][
            "premium_balance"
        ]
    ])

    calibration_features[
        "option_oi"
    ] = mean([
        features["options"]["NIFTY"][
            "oi_balance"
        ],
        features["options"]["SENSEX"][
            "oi_balance"
        ]
    ])

    calibration_features[
        "option_imbalance"
    ] = mean([
        features["options"]["NIFTY"][
            "bid_ask_balance"
        ],
        features["options"]["SENSEX"][
            "bid_ask_balance"
        ]
    ])

    calibration_features[
        "index_return"
    ] = mean([
        features["indices"]["NIFTY"][
            "return_pct"
        ] / 0.40,
        features["indices"]["SENSEX"][
            "return_pct"
        ] / 0.40
    ])

    calibration_features[
        "index_imbalance"
    ] = mean([
        features["indices"]["NIFTY"][
            "imbalance"
        ],
        features["indices"]["SENSEX"][
            "imbalance"
        ]
    ])

    calibration_features[
        "heavyweights"
    ] = features[
        "heavyweights"
    ][
        "average_score"
    ]

    calibration_features[
        "momentum"
    ] = mean([
        features["window"]["NIFTY"][
            "momentum"
        ] / 0.20,
        features["window"]["SENSEX"][
            "momentum"
        ] / 0.20
    ])

    record = {
        "timestamp":
            timestamp(),
        "prediction_time":
            state[
                "prediction_time"
            ],
        "baseline_time":
            baseline[
                "timestamp"
            ],
        "outcome_time":
            outcome_snapshot[
                "timestamp"
            ],
        "priority":
            priority,
        "prediction":
            signal[
                "direction"
            ],
        "confidence":
            signal[
                "confidence"
            ],
        "nifty_score":
            signal[
                "nifty_score"
            ],
        "sensex_score":
            signal[
                "sensex_score"
            ],
        "outcome_direction":
            outcome[
                "direction"
            ],
        "outcome_move_pct":
            outcome[
                "move_pct"
            ],
        "correct":
            evaluation[
                "correct"
            ],
        "evaluation":
            evaluation[
                "evaluation"
            ],
        "features":
            calibration_features,
        "regime":
            features[
                "regime"
            ],
        "sample_count":
            features[
                "sample_count"
            ]
    }

    save_calibration(
        record
    )

    # ----------------------------------------
    # Historical calibration status.
    # ----------------------------------------

    rows = load_calibration()

    accuracy = (
        historical_direction_accuracy(
            rows
        )
    )

    # ----------------------------------------
    # Send outcome.
    # ----------------------------------------

    outcome_message = (
        format_outcome_message(
            signal,
            outcome,
            evaluation
        )
    )

    try:
        telegram_send(
            outcome_message
        )

    except Exception as exc:
        print(
            "Telegram outcome error:",
            exc
        )

    # ----------------------------------------
    # Final console report.
    # ----------------------------------------

    print(
        "=" * 60
    )

    print(
        "CAS SESSION COMPLETE"
    )

    print(
        "Prediction:",
        signal["direction"]
    )

    print(
        "Confidence:",
        signal["confidence"]
    )

    print(
        "Actual:",
        outcome["direction"]
    )

    print(
        "15:15 -> 15:30:",
        f"{outcome['move_pct']:+.2f}%"
    )

    if accuracy is not None:
        print(
            "Historical accuracy:",
            f"{accuracy * 100:.1f}%"
        )
    else:
        print(
            "Historical accuracy:",
            "Not enough samples"
        )

    print(
        "Calibration samples:",
        len(rows)
    )

    print(
        "=" * 60
    )


if __name__ == "__main__":
    try:
        main()

    except Exception as exc:
        print(
            "FATAL CAS ENGINE ERROR:",
            repr(exc)
        )

        # Try to notify Telegram.
        try:
            telegram_send(
                "🚨 CAS ENGINE ERROR\n\n"
                + str(exc)[:3500]
            )
        except Exception:
            pass

        raise
