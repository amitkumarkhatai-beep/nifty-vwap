import os
import csv
import json
import math
import time
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

import requests
import pyotp


# ============================================================
# CAS SIGNAL ENGINE
# 3:00 PM -> 3:17 PM collection
# 3:17 PM -> prediction locked
# 3:30 PM -> outcome + calibration
# ============================================================

IST = ZoneInfo("Asia/Kolkata")

DHAN_CLIENT_ID = os.environ["DHAN_CLIENT_ID"]
DHAN_PIN = os.environ["DHAN_PIN"]
DHAN_TOTP_SECRET = os.environ["DHAN_TOTP_SECRET"]

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "1001276343")

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

# Dhan index IDs.
# NIFTY = 13 is documented by Dhan.
# SENSEX can be overridden through GitHub environment variables.
NIFTY_ID = int(os.environ.get("DHAN_NIFTY_ID", "13"))
SENSEX_ID = int(os.environ.get("DHAN_SENSEX_ID", "51"))

INDEX_SEGMENT = "IDX_I"

API = "https://api.dhan.co/v2"

DATA_DIR = "data"
CALIBRATION_FILE = os.path.join(DATA_DIR, "calibration.jsonl")


# ============================================================
# GENERAL HELPERS
# ============================================================

def now_ist():
    return datetime.now(IST)


def sleep_until(target):
    while True:
        remaining = (target - now_ist()).total_seconds()

        if remaining <= 0:
            return

        time.sleep(min(remaining, 2))


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
    return max(low, min(high, value))


def pct_change(a, b):
    if not a:
        return 0.0
    return ((b - a) / a) * 100.0


def mean(values):
    values = [x for x in values if x is not None]
    if not values:
        return 0.0
    return sum(values) / len(values)


# ============================================================
# DHAN AUTHENTICATION
# ============================================================

def dhan_access_token():
    """
    Generate a fresh 24-hour Dhan access token using TOTP.
    """

    totp = pyotp.TOTP(DHAN_TOTP_SECRET).now()

    url = (
        "https://auth.dhan.co/app/generateAccessToken"
        f"?dhanClientId={DHAN_CLIENT_ID}"
        f"&pin={DHAN_PIN}"
        f"&totp={totp}"
    )

    response = requests.post(url, timeout=20)
    response.raise_for_status()

    data = response.json()

    token = data.get("accessToken")

    if not token:
        raise RuntimeError(f"Dhan authentication failed: {data}")

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

    def post(self, endpoint, payload):
        url = API + endpoint

        response = requests.post(
            url,
            headers=self.headers,
            json=payload,
            timeout=20,
        )

        if response.status_code >= 400:
            raise RuntimeError(
                f"Dhan API {endpoint} "
                f"{response.status_code}: {response.text[:1000]}"
            )

        return response.json()

    def expiry_list(self, security_id, segment):
        return self.post(
            "/optionchain/expirylist",
            {
                "UnderlyingScrip": security_id,
                "UnderlyingSeg": segment,
            },
        )

    def option_chain(self, security_id, segment, expiry):
        return self.post(
            "/optionchain",
            {
                "UnderlyingScrip": security_id,
                "UnderlyingSeg": segment,
                "Expiry": expiry,
            },
        )

    def quote(self, securities):
        return self.post(
            "/marketfeed/quote",
            securities,
        )


# ============================================================
# DHAN RESPONSE NORMALIZATION
# ============================================================

def unwrap_data(response):
    if not isinstance(response, dict):
        return {}

    return response.get("data", response)


def extract_expiries(response):
    data = unwrap_data(response)

    if isinstance(data, list):
        return [str(x) for x in data]

    for key in ("expiry", "expiries", "Expiry", "ExpiryList"):
        if key in data and isinstance(data[key], list):
            return [str(x) for x in data[key]]

    return []


def first_valid_expiry(dhan, security_id):
    response = dhan.expiry_list(
        security_id,
        INDEX_SEGMENT,
    )

    expiries = extract_expiries(response)

    if not expiries:
        raise RuntimeError(
            f"No expiry returned for underlying {security_id}: {response}"
        )

    today = now_ist().date()

    parsed = []

    for value in expiries:
        try:
            d = datetime.strptime(value[:10], "%Y-%m-%d").date()
            parsed.append((d, value))
        except Exception:
            continue

    future = [x for x in parsed if x[0] >= today]

    if future:
        future.sort()
        return future[0][1]

    return expiries[0]


# ============================================================
# OPTION CHAIN PARSING
# ============================================================

def find_chain_rows(response):
    data = unwrap_data(response)

    # Dhan commonly returns a strike-wise dictionary.
    if "oc" in data and isinstance(data["oc"], dict):
        return data["oc"]

    if "data" in data and isinstance(data["data"], dict):
        if "oc" in data["data"]:
            return data["data"]["oc"]

    return data


def parse_option_rows(response):
    rows = find_chain_rows(response)

    output = []

    if not isinstance(rows, dict):
        return output

    for strike_key, item in rows.items():

        if not isinstance(item, dict):
            continue

        strike = safe_float(
            item.get("strike_price", strike_key)
        )

        ce = item.get("ce") or item.get("CE") or {}
        pe = item.get("pe") or item.get("PE") or {}

        if ce:
            output.append({
                "strike": strike,
                "side": "CE",
                **ce,
            })

        if pe:
            output.append({
                "strike": strike,
                "side": "PE",
                **pe,
            })

    return output


def build_atm_contracts(dhan, security_id, label):
    expiry = first_valid_expiry(
        dhan,
        security_id,
    )

    chain = dhan.option_chain(
        security_id,
        INDEX_SEGMENT,
        expiry,
    )

    rows = parse_option_rows(chain)

    if not rows:
        raise RuntimeError(
            f"No option-chain rows for {label}"
        )

    spot = safe_float(
        unwrap_data(chain).get("last_price", 0)
    )

    if spot <= 0:
        # Calculate approximate spot from option strikes
        # only as fallback.
        strikes = [
            r["strike"]
            for r in rows
            if r.get("strike")
        ]

        if not strikes:
            raise RuntimeError(
                f"Unable to determine spot for {label}"
            )

        spot = mean(strikes)

    strikes = sorted(
        set(
            r["strike"]
            for r in rows
            if r.get("strike")
        )
    )

    atm = min(
        strikes,
        key=lambda x: abs(x - spot)
    )

    nearby = sorted(
        strikes,
        key=lambda x: abs(x - atm)
    )[:3]

    contracts = []

    for strike in nearby:
        for side in ("CE", "PE"):

            match = next(
                (
                    r for r in rows
                    if r["strike"] == strike
                    and r["side"] == side
                ),
                None,
            )

            if not match:
                continue

            security = (
                match.get("security_id")
                or match.get("SecurityId")
            )

            if not security:
                continue

            contracts.append({
                "label": label,
                "expiry": expiry,
                "strike": strike,
                "side": side,
                "security_id": str(security),
                "segment": "NSE_FNO" if label == "NIFTY" else "BSE_FNO",
            })

    if len(contracts) < 6:
        raise RuntimeError(
            f"Could not build ATM ±1 contracts for {label}"
        )

    return {
        "label": label,
        "spot": spot,
        "atm": atm,
        "expiry": expiry,
        "contracts": contracts,
    }


# ============================================================
# MARKET QUOTE
# ============================================================

def quote_to_map(response):
    data = unwrap_data(response)

    result = {}

    if not isinstance(data, dict):
        return result

    for segment, instruments in data.items():

        if not isinstance(instruments, dict):
            continue

        for security_id, packet in instruments.items():

            if not isinstance(packet, dict):
                continue

            result[str(security_id)] = {
                "segment": segment,
                **packet,
            }

    return result


def get_quote(dhan, instruments):
    grouped = {}

    for instrument in instruments:
        segment = instrument["segment"]
        sid = int(instrument["security_id"])

        grouped.setdefault(segment, [])

        if sid not in grouped[segment]:
            grouped[segment].append(sid)

    return quote_to_map(
        dhan.quote(grouped)
    )


def packet_ltp(packet):
    return safe_float(
        packet.get("last_price")
        or packet.get("ltp")
    )


def packet_volume(packet):
    return safe_float(
        packet.get("volume")
    )


def packet_oi(packet):
    return safe_float(
        packet.get("oi")
    )


def packet_bid_qty(packet):
    return safe_float(
        packet.get("buy_quantity")
        or packet.get("total_buy_quantity")
    )


def packet_ask_qty(packet):
    return safe_float(
        packet.get("sell_quantity")
        or packet.get("total_sell_quantity")
    )


# ============================================================
# MARKET DEPTH / ORDER FLOW
# ============================================================

def depth_imbalance(packet):

    buy = packet_bid_qty(packet)
    sell = packet_ask_qty(packet)

    total = buy + sell

    if total <= 0:
        return 0.0

    return clamp(
        (buy - sell) / total
    )


def volume_delta(previous, current):
    if current < previous:
        # Session reset/data correction.
        return 0.0

    return max(0.0, current - previous)


# ============================================================
# HEAVYWEIGHT UNIVERSE
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


def load_nse_instrument_master():
    """
    Load Dhan's public detailed instrument master.

    This is used only for security IDs.
    """

    url = (
        "https://images.dhan.co/api-data/"
        "api-scrip-master-detailed.csv"
    )

    response = requests.get(
        url,
        timeout=30,
    )

    response.raise_for_status()

    rows = csv.DictReader(
        response.text.splitlines()
    )

    result = {}

    for row in rows:

        symbol = (
            row.get("SEM_TRADING_SYMBOL")
            or row.get("SEM_CUSTOM_SYMBOL")
            or row.get("trading_symbol")
            or ""
        ).strip()

        segment = (
            row.get("SEM_EXM_EXCH_ID")
            or row.get("SEM_SEGMENT")
            or ""
        ).upper()

        security_id = (
            row.get("SEM_SMST_SECURITY_ID")
            or row.get("security_id")
            or ""
        )

        if not symbol or not security_id:
            continue

        if segment not in ("NSE", "NSE_EQ"):
            continue

        clean = symbol.upper()

        if clean in NIFTY_HEAVYWEIGHTS:
            result[clean] = {
                "security_id": str(security_id),
                "segment": "NSE_EQ",
            }

    return result


def build_heavyweight_instruments():
    try:
        master = load_nse_instrument_master()
    except Exception as exc:
        print(
            "Heavyweight master unavailable:",
            exc,
        )
        return []

    instruments = []

    for symbol in NIFTY_HEAVYWEIGHTS:

        if symbol not in master:
            continue

        instruments.append({
            "symbol": symbol,
            "security_id": master[symbol]["security_id"],
            "segment": "NSE_EQ",
        })

    return instruments


# ============================================================
# SNAPSHOT COLLECTION
# ============================================================

def make_snapshot(
    quote,
    index_instruments,
    option_contracts,
    heavyweight_instruments,
):

    snapshot = {
        "timestamp": now_ist().isoformat(),
        "indices": {},
        "options": {},
        "heavyweights": {},
    }

    for instrument in index_instruments:

        sid = instrument["security_id"]

        packet = quote.get(
            sid,
            {},
        )

        snapshot["indices"][instrument["label"]] = {
            "ltp": packet_ltp(packet),
            "volume": packet_volume(packet),
            "oi": packet_oi(packet),
            "imbalance": depth_imbalance(packet),
            "buy_qty": packet_bid_qty(packet),
            "sell_qty": packet_ask_qty(packet),
        }

    for contract in option_contracts:

        sid = contract["security_id"]

        packet = quote.get(
            sid,
            {},
        )

        key = (
            f'{contract["label"]}_'
            f'{contract["strike"]}_'
            f'{contract["side"]}'
        )

        snapshot["options"][key] = {
            "label": contract["label"],
            "strike": contract["strike"],
            "side": contract["side"],
            "security_id": sid,
            "ltp": packet_ltp(packet),
            "volume": packet_volume(packet),
            "oi": packet_oi(packet),
            "imbalance": depth_imbalance(packet),
            "buy_qty": packet_bid_qty(packet),
            "sell_qty": packet_ask_qty(packet),
        }

    for stock in heavyweight_instruments:

        sid = stock["security_id"]

        packet = quote.get(
            sid,
            {},
        )

        snapshot["heavyweights"][
            stock["symbol"]
        ] = {
            "ltp": packet_ltp(packet),
            "volume": packet_volume(packet),
            "imbalance": depth_imbalance(packet),
        }

    return snapshot


# ============================================================
# FEATURE ENGINEERING
# ============================================================

def option_features(
    snapshots,
    label,
):
    rows = []

    for snapshot in snapshots:

        for key, option in snapshot["options"].items():

            if option["label"] != label:
                continue

            rows.append(
                (
                    snapshot["timestamp"],
                    option,
                )
            )

    if not rows:
        return {
            "ce_volume": 0,
            "pe_volume": 0,
            "ce_acceleration": 0,
            "pe_acceleration": 0,
            "ce_price_change": 0,
            "pe_price_change": 0,
            "ce_oi_change": 0,
            "pe_oi_change": 0,
            "ce_pe_volume_ratio": 1,
            "flow_score": 0,
        }

    first = {}
    last = {}

    for _, option in rows:

        key = (
            option["strike"],
            option["side"],
        )

        if key not in first:
            first[key] = option

        last[key] = option

    ce_volume = 0
    pe_volume = 0

    ce_price_changes = []
    pe_price_changes = []

    ce_oi_changes = []
    pe_oi_changes = []

    ce_acceleration = 0
    pe_acceleration = 0

    for key in first:

        f = first[key]
        l = last[key]

        volume_change = volume_delta(
            f["volume"],
            l["volume"],
        )

        price_change = pct_change(
            f["ltp"],
            l["ltp"],
        )

        oi_change = l["oi"] - f["oi"]

        if key[1] == "CE":

            ce_volume += volume_change
            ce_price_changes.append(
                price_change
            )
            ce_oi_changes.append(
                oi_change
            )

        else:

            pe_volume += volume_change
            pe_price_changes.append(
                price_change
            )
            pe_oi_changes.append(
                oi_change
            )

    # Compare first half vs second half volume velocity.
    midpoint = len(snapshots) // 2

    first_half = snapshots[:midpoint]
    second_half = snapshots[midpoint:]

    def side_volume(data, side):
        total = 0

        for snap in data:

            for option in snap["options"].values():

                if (
                    option["label"] == label
                    and option["side"] == side
                ):
                    total += option["volume"]

        return total

    ce_first = side_volume(
        first_half,
        "CE",
    )

    ce_second = side_volume(
        second_half,
        "CE",
    )

    pe_first = side_volume(
        first_half,
        "PE",
    )

    pe_second = side_volume(
        second_half,
        "PE",
    )

    ce_acceleration = (
        (ce_second / max(1, len(second_half)))
        -
        (ce_first / max(1, len(first_half)))
    )

    pe_acceleration = (
        (pe_second / max(1, len(second_half)))
        -
        (pe_first / max(1, len(first_half)))
    )

    ratio = (
        pe_volume / max(1.0, ce_volume)
    )

    # For a directional option-flow signal:
    # PE buying / PE volume expansion supports bearishness.
    # CE buying / CE volume expansion supports bullishness.
    flow = (
        (pe_volume - ce_volume)
        / max(
            1.0,
            pe_volume + ce_volume,
        )
    )

    acceleration = (
        (pe_acceleration - ce_acceleration)
        / max(
            1.0,
            abs(pe_acceleration)
            + abs(ce_acceleration),
        )
    )

    flow_score = clamp(
        0.65 * flow
        + 0.35 * acceleration
    )

    return {
        "ce_volume": ce_volume,
        "pe_volume": pe_volume,
        "ce_pe_volume_ratio": ratio,
        "ce_acceleration": ce_acceleration,
        "pe_acceleration": pe_acceleration,
        "ce_price_change": mean(
            ce_price_changes
        ),
        "pe_price_change": mean(
            pe_price_changes
        ),
        "ce_oi_change": sum(
            ce_oi_changes
        ),
        "pe_oi_change": sum(
            pe_oi_changes
        ),
        "flow_score": flow_score,
    }


def index_features(
    snapshots,
    label,
):
    values = [
        s["indices"][label]
        for s in snapshots
        if label in s["indices"]
    ]

    if not values:
        return {
            "return": 0,
            "imbalance": 0,
            "imbalance_change": 0,
            "volume_change": 0,
        }

    first = values[0]
    last = values[-1]

    return {
        "return": pct_change(
            first["ltp"],
            last["ltp"],
        ),
        "imbalance": last["imbalance"],
        "imbalance_change": (
            last["imbalance"]
            - first["imbalance"]
        ),
        "volume_change": (
            last["volume"]
            - first["volume"]
        ),
    }


def heavyweight_features(
    snapshots,
):
    names = set()

    for snapshot in snapshots:
        names.update(
            snapshot["heavyweights"].keys()
        )

    if not names:
        return {
            "average_imbalance": 0,
            "confirming": 0,
            "total": 0,
        }

    imbalances = []
    confirming = 0

    for name in names:

        series = []

        for snapshot in snapshots:

            item = snapshot[
                "heavyweights"
            ].get(name)

            if item:
                series.append(item)

        if not series:
            continue

        first = series[0]
        last = series[-1]

        imbalance = last["imbalance"]

        price_move = pct_change(
            first["ltp"],
            last["ltp"],
        )

        score = (
            0.60 * imbalance
            + 0.40 * clamp(
                price_move / 0.30
            )
        )

        imbalances.append(score)

        if abs(score) >= 0.15:
            confirming += 1

    return {
        "average_imbalance": mean(
            imbalances
        ),
        "confirming": confirming,
        "total": len(imbalances),
    }


def structure_features(
    snapshots,
    label,
):
    values = [
        s["indices"][label]["ltp"]
        for s in snapshots
        if label in s["indices"]
    ]

    if not values:
        return {
            "return": 0,
            "high": 0,
            "low": 0,
            "range": 0,
        }

    return {
        "return": pct_change(
            values[0],
            values[-1],
        ),
        "high": max(values),
        "low": min(values),
        "range": (
            max(values) - min(values)
        ),
    }


# ============================================================
# QUANTITATIVE SIGNAL
# ============================================================

FEATURE_NAMES = [
    "option_flow",
    "option_acceleration",
    "index_return",
    "index_imbalance",
    "imbalance_change",
    "heavyweight_confirmation",
]


DEFAULT_WEIGHTS = {
    "option_flow": 0.26,
    "option_acceleration": 0.12,
    "index_return": 0.18,
    "index_imbalance": 0.18,
    "imbalance_change": 0.10,
    "heavyweight_confirmation": 0.16,
}


def feature_vector(
    option_data,
    index_data,
    heavyweight_data,
):

    return {
        "option_flow": clamp(
            option_data["flow_score"]
        ),

        "option_acceleration": clamp(
            (
                option_data["pe_acceleration"]
                -
                option_data["ce_acceleration"]
            )
            /
            max(
                1.0,
                abs(
                    option_data[
                        "pe_acceleration"
                    ]
                )
                +
                abs(
                    option_data[
                        "ce_acceleration"
                    ]
                ),
            )
        ),

        "index_return": clamp(
            index_data["return"] / 0.35
        ),

        "index_imbalance": clamp(
            index_data["imbalance"]
        ),

        "imbalance_change": clamp(
            index_data["imbalance_change"]
            * 3
        ),

        "heavyweight_confirmation": clamp(
            heavyweight_data[
                "average_imbalance"
            ]
        ),
    }


def weighted_score(
    features,
    weights,
):
    score = 0.0

    for name in FEATURE_NAMES:
        score += (
            features[name]
            * weights.get(
                name,
                DEFAULT_WEIGHTS[name],
            )
        )

    return clamp(score)


# ============================================================
# WALK-FORWARD CALIBRATION
# ============================================================

def ensure_data_dir():
    os.makedirs(
        DATA_DIR,
        exist_ok=True,
    )


def load_history():
    ensure_data_dir()

    if not os.path.exists(
        CALIBRATION_FILE
    ):
        return []

    records = []

    with open(
        CALIBRATION_FILE,
        "r",
        encoding="utf-8",
    ) as f:

        for line in f:

            try:
                records.append(
                    json.loads(line)
                )
            except Exception:
                pass

    return records


def save_record(record):
    ensure_data_dir()

    with open(
        CALIBRATION_FILE,
        "a",
        encoding="utf-8",
    ) as f:

        f.write(
            json.dumps(
                record,
                separators=(",", ":"),
            )
            + "\n"
        )


def calibrated_weights(history):

    if len(history) < 25:
        return DEFAULT_WEIGHTS.copy()

    weights = DEFAULT_WEIGHTS.copy()

    # Simple bounded walk-forward adjustment.
    # We deliberately require a meaningful sample
    # before changing the weights.

    for name in FEATURE_NAMES:

        correct_positive = 0
        correct_negative = 0
        total = 0

        for record in history[-150:]:

            outcome = record.get(
                "outcome"
            )

            features = record.get(
                "features",
                {},
            )

            if outcome not in (
                "BULLISH",
                "BEARISH",
            ):
                continue

            value = safe_float(
                features.get(name)
            )

            if value == 0:
                continue

            predicted_positive = (
                value > 0
            )

            actual_positive = (
                outcome == "BULLISH"
            )

            if predicted_positive == actual_positive:
                correct_positive += 1

            correct_negative += 0
            total += 1

        if total >= 20:

            accuracy = (
                correct_positive
                / total
            )

            multiplier = (
                0.75
                +
                0.60 * accuracy
            )

            weights[name] = (
                DEFAULT_WEIGHTS[name]
                * multiplier
            )

    total_weight = sum(
        weights.values()
    )

    if total_weight <= 0:
        return DEFAULT_WEIGHTS.copy()

    return {
        k: v / total_weight
        for k, v in weights.items()
    }


# ============================================================
# OPTIONAL AI LAYER
# ============================================================

def ai_assessment(
    features,
    quant_score,
    priority,
    confidence,
):

    if not OPENAI_API_KEY:
        return {
            "direction": (
                "BULLISH"
                if quant_score > 0
                else "BEARISH"
                if quant_score < 0
                else "NO SIGNAL"
            ),
            "confidence": confidence,
            "available": False,
            "reason": "AI disabled",
        }

    prompt = {
        "task": "CAS market classification",
        "instruction": (
            "Classify the 3:17 PM CAS direction. "
            "Use only the supplied quantitative data. "
            "Do not invent data. "
            "If evidence conflicts, return NO SIGNAL."
        ),
        "priority": priority,
        "quant_score": quant_score,
        "quant_confidence": confidence,
        "features": features,
        "allowed": [
            "BULLISH",
            "BEARISH",
            "NO SIGNAL",
        ],
    }

    try:

        response = requests.post(
            "https://api.openai.com/v1/responses",
            headers={
                "Authorization":
                    f"Bearer {OPENAI_API_KEY}",
                "Content-Type":
                    "application/json",
            },
            json={
                "model": os.environ.get(
                    "OPENAI_MODEL",
                    "gpt-5.6-luna",
                ),
                "input": json.dumps(
                    prompt
                ),
            },
            timeout=30,
        )

        response.raise_for_status()

        data = response.json()

        text = data.get(
            "output_text",
            "",
        ).strip().upper()

        if "BULLISH" in text:
            direction = "BULLISH"
        elif "BEARISH" in text:
            direction = "BEARISH"
        else:
            direction = "NO SIGNAL"

        return {
            "direction": direction,
            "confidence": confidence,
            "available": True,
            "reason": text[:300],
        }

    except Exception as exc:

        print(
            "AI unavailable:",
            exc,
        )

        return {
            "direction": "NO SIGNAL",
            "confidence": confidence,
            "available": False,
            "reason": "AI request failed",
        }


# ============================================================
# FINAL SIGNAL
# ============================================================

def classify(
    score,
    ai_direction,
    priority,
):

    # Quantitative score remains authoritative.
    # AI acts as confirmation.

    if abs(score) < 0.18:
        return "NO SIGNAL"

    quant_direction = (
        "BULLISH"
        if score > 0
        else "BEARISH"
    )

    if ai_direction == "NO SIGNAL":
        return "NO SIGNAL"

    # Require AI agreement for stronger signals.
    if ai_direction != quant_direction:
        if abs(score) < 0.42:
            return "NO SIGNAL"

        return quant_direction

    return quant_direction


def confidence_from_score(
    score,
    historical_count,
):

    base = 50 + (
        min(
            40,
            abs(score) * 45,
        )
    )

    if historical_count < 25:
        base -= 5

    return int(
        max(
            50,
            min(
                90,
                base,
            ),
        )
    )


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message):

    url = (
        f"https://api.telegram.org/bot"
        f"{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    response = requests.post(
        url,
        data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
        },
        timeout=20,
    )

    if response.status_code >= 400:
        raise RuntimeError(
            f"Telegram failed: "
            f"{response.text}"
        )


# ============================================================
# FORMAT SIGNAL
# ============================================================

def arrow(direction):

    if direction == "BULLISH":
        return "🟢 BULLISH"

    if direction == "BEARISH":
        return "🔴 BEARISH"

    return "🟡 NO CLEAR SIGNAL"


def format_signal(
    results,
    priority,
    final_direction,
    final_confidence,
):

    lines = [
        "🚨 CAS SIGNAL | 3:17 PM",
        "",
    ]

    for label in ("NIFTY", "SENSEX"):

        if label not in results:
            continue

        r = results[label]

        lines.append(
            f"📊 {label}: "
            f"{arrow(r['direction'])}"
        )

        lines.append(
            f"ATM: {r['atm']} | "
            f"Expiry: {r['expiry']}"
        )

        lines.append(
            f"CE/PE Volume Ratio: "
            f"{r['ce_pe_ratio']:.2f}"
        )

        lines.append(
            f"Option Flow: "
            f"{r['option_flow']:+.2f}"
        )

        lines.append(
            f"Bid/Ask Imbalance: "
            f"{r['imbalance']:+.2f}"
        )

        lines.append(
            f"Spot Return: "
            f"{r['spot_return']:+.2f}%"
        )

        lines.append(
            f"Heavyweight Confirmation: "
            f"{r['heavyweight_confirming']}/"
            f"{r['heavyweight_total']}"
        )

        lines.append("")

    lines.extend([
        f"🔥 Priority: {priority}",
        "",
        f"🎯 CAS Bias: "
        f"{arrow(final_direction)}",
        f"Confidence: {final_confidence}%",
        "",
        "#Nifty #Sensex #CAS "
        "#OptionsTrading #OrderFlow "
        "#IndianStockMarket",
    ])

    return "\n".join(lines)


# ============================================================
# COLLECTION
# ============================================================

def collect_window(
    dhan,
    index_instruments,
    option_contracts,
    heavyweight_instruments,
):

    start = now_ist().replace(
        hour=15,
        minute=0,
        second=0,
        microsecond=0,
    )

    end = now_ist().replace(
        hour=15,
        minute=17,
        second=0,
        microsecond=0,
    )

    sleep_until(start)

    snapshots = []

    while now_ist() < end:

        try:

            instruments = (
                index_instruments
                + option_contracts
                + heavyweight_instruments
            )

            quote = get_quote(
                dhan,
                instruments,
            )

            snapshot = make_snapshot(
                quote,
                index_instruments,
                option_contracts,
                heavyweight_instruments,
            )

            snapshots.append(snapshot)

            print(
                "Collected:",
                snapshot["timestamp"],
            )

        except Exception as exc:

            print(
                "Collection error:",
                exc,
            )

        time.sleep(1.05)

    return snapshots


# ============================================================
# PROCESS ONE INDEX
# ============================================================

def process_index(
    label,
    snapshots,
    contract_info,
    history,
):

    option_data = option_features(
        snapshots,
        label,
    )

    index_data = index_features(
        snapshots,
        label,
    )

    hw_data = heavyweight_features(
        snapshots,
    )

    structure = structure_features(
        snapshots,
        label,
    )

    features = feature_vector(
        option_data,
        index_data,
        hw_data,
    )

    weights = calibrated_weights(
        history
    )

    score = weighted_score(
        features,
        weights,
    )

    confidence = confidence_from_score(
        score,
        len(history),
    )

    # AI confirmation.
    priority = label

    ai = ai_assessment(
        features,
        score,
        priority,
        confidence,
    )

    direction = classify(
        score,
        ai["direction"],
        priority,
    )

    return {
        "label": label,
        "direction": direction,
        "score": score,
        "confidence": confidence,
        "atm": contract_info["atm"],
        "expiry": contract_info["expiry"],
        "ce_pe_ratio": option_data[
            "ce_pe_volume_ratio"
        ],
        "option_flow": option_data[
            "flow_score"
        ],
        "imbalance": index_data[
            "imbalance"
        ],
        "spot_return": index_data[
            "return"
        ],
        "heavyweight_confirming":
            hw_data["confirming"],
        "heavyweight_total":
            hw_data["total"],
        "features": features,
        "structure": structure,
        "ai": ai,
    }


# ============================================================
# 3:15 BASELINE
# ============================================================

def baseline_at_1515(
    dhan,
    index_instruments,
):

    target = now_ist().replace(
        hour=15,
        minute=15,
        second=0,
        microsecond=0,
    )

    sleep_until(target)

    quote = get_quote(
        dhan,
        index_instruments,
    )

    baseline = {}

    for instrument in index_instruments:

        packet = quote.get(
            instrument["security_id"],
            {},
        )

        baseline[
            instrument["label"]
        ] = packet_ltp(packet)

    return baseline


# ============================================================
# 3:30 OUTCOME
# ============================================================

def outcome_at_1530(
    dhan,
    index_instruments,
    baseline,
):

    target = now_ist().replace(
        hour=15,
        minute=30,
        second=0,
        microsecond=0,
    )

    sleep_until(target)

    quote = get_quote(
        dhan,
        index_instruments,
    )

    outcomes = {}

    for instrument in index_instruments:

        label = instrument["label"]

        packet = quote.get(
            instrument["security_id"],
            {},
        )

        final_price = packet_ltp(
            packet
        )

        base = baseline.get(
            label,
            0,
        )

        move = pct_change(
            base,
            final_price,
        )

        # Meaningful threshold:
        # avoids treating tiny noise as direction.
        if move > 0.05:
            direction = "BULLISH"

        elif move < -0.05:
            direction = "BEARISH"

        else:
            direction = "NEUTRAL"

        outcomes[label] = {
            "baseline_1515": base,
            "price_1530": final_price,
            "move_pct": move,
            "direction": direction,
        }

    return outcomes


# ============================================================
# MAIN
# ============================================================

def main():

    print(
        "Starting CAS engine:",
        now_ist().isoformat(),
    )

    ensure_data_dir()

    dhan = Dhan()

    index_instruments = [
        {
            "label": "NIFTY",
            "security_id": str(
                NIFTY_ID
            ),
            "segment": INDEX_SEGMENT,
        },
        {
            "label": "SENSEX",
            "security_id": str(
                SENSEX_ID
            ),
            "segment": INDEX_SEGMENT,
        },
    ]

    # Build ATM ±1 option universe before 3 PM.
    print(
        "Building option contracts..."
    )

    nifty_contracts = build_atm_contracts(
        dhan,
        NIFTY_ID,
        "NIFTY",
    )

    time.sleep(3.2)

    sensex_contracts = build_atm_contracts(
        dhan,
        SENSEX_ID,
        "SENSEX",
    )

    option_contracts = (
        nifty_contracts["contracts"]
        +
        sensex_contracts["contracts"]
    )

    heavyweight_instruments = (
        build_heavyweight_instruments()
    )

    print(
        "Heavyweights:",
        len(heavyweight_instruments),
    )

    # 3:00-3:17 collection.
    snapshots = collect_window(
        dhan,
        index_instruments,
        option_contracts,
        heavyweight_instruments,
    )

    if len(snapshots) < 30:
        raise RuntimeError(
            "Too few market snapshots "
            "were collected."
        )

    print(
        "Snapshots collected:",
        len(snapshots),
    )

    history = load_history()

    # Determine expiry priority.
    today = now_ist().date()

    nifty_expiry = datetime.strptime(
        nifty_contracts["expiry"][:10],
        "%Y-%m-%d",
    ).date()

    sensex_expiry = datetime.strptime(
        sensex_contracts["expiry"][:10],
        "%Y-%m-%d",
    ).date()

    if nifty_expiry == today:
        priority = "NIFTY"

    elif sensex_expiry == today:
        priority = "SENSEX"

    else:
        priority = "NIFTY"

    # Process both markets.
    results = {}

    results["NIFTY"] = process_index(
        "NIFTY",
        snapshots,
        nifty_contracts,
        history,
    )

    results["SENSEX"] = process_index(
        "SENSEX",
        snapshots,
        sensex_contracts,
        history,
    )

    # Priority market gets greater authority.
    p = results[priority]

    if p["direction"] == "NO SIGNAL":

        final_direction = "NO SIGNAL"

    else:

        # Require meaningful score.
        if abs(p["score"]) < 0.18:
            final_direction = "NO SIGNAL"
        else:
            final_direction = p["direction"]

    final_confidence = p["confidence"]

    # Save the 3:17 prediction data in memory.
    prediction = {
        "timestamp": now_ist().isoformat(),
        "prediction_time": now_ist().isoformat(),
        "priority": priority,
        "direction": final_direction,
        "confidence": final_confidence,
        "features": p["features"],
        "results": results,
    }

    message = format_signal(
        results,
        priority,
        final_direction,
        final_confidence,
    )

    print(message)

    send_telegram(message)

    # --------------------------------------------------------
    # 3:15 baseline
    # --------------------------------------------------------

    baseline = baseline_at_1515(
        dhan,
        index_instruments,
    )

    # --------------------------------------------------------
    # 3:30 evaluation
    # --------------------------------------------------------

    outcomes = outcome_at_1530(
        dhan,
        index_instruments,
        baseline,
    )

    actual = outcomes.get(
        priority,
        {},
    )

    actual_direction = actual.get(
        "direction",
        "NEUTRAL",
    )

    if (
        final_direction in (
            "BULLISH",
            "BEARISH",
        )
        and actual_direction in (
            "BULLISH",
            "BEARISH",
        )
    ):

        correct = (
            final_direction
            == actual_direction
        )

    else:
        correct = None

    record = {
        "date": now_ist().strftime(
            "%Y-%m-%d"
        ),
        "timestamp": now_ist().isoformat(),
        "priority": priority,
        "prediction": final_direction,
        "confidence": final_confidence,
        "actual": actual_direction,
        "correct": correct,
        "outcome_move_pct":
            actual.get(
                "move_pct",
                0,
            ),
        "features": p["features"],
        "score": p["score"],
    }

    save_record(record)

    print(
        "Calibration record saved:",
        json.dumps(
            record,
            indent=2,
        ),
    )

    # Send evaluation update.
    if correct is True:
        result_text = "✅ Prediction confirmed"

    elif correct is False:
        result_text = "❌ Prediction failed"

    else:
        result_text = "🟡 Neutral / not scored"

    evaluation = (
        "\n\n📈 CAS 3:30 EVALUATION\n"
        f"Priority: {priority}\n"
        f"Prediction: {final_direction}\n"
        f"3:15 → 3:30: "
        f"{actual.get('move_pct', 0):+.2f}%\n"
        f"Outcome: {actual_direction}\n"
        f"{result_text}"
    )

    send_telegram(
        message + evaluation
    )

    print(
        "CAS completed:",
        now_ist().isoformat(),
    )


if __name__ == "__main__":
    main()
