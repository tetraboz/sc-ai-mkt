"""Daily and five minute market data, with exchange times in JST."""
import csv
import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from math import isfinite, prod
from pathlib import Path
from statistics import median
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")
STOCKS = ("5381", "5542", "5367", "3446")
ETF = "200A"
DECISION = time(10, 30)
OBSERVATIONS = (time(9, 30), time(10))
FIVE_MINUTES = timedelta(minutes=5)


def timestamp(value):
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        raise ValueError(f"Time zone required: {value}")
    return stamp.astimezone(JST)


def at(day, hhmm):
    return datetime.combine(day, hhmm, JST)


def epoch_day(value):
    return datetime.fromtimestamp(value, JST).date()


@dataclass(frozen=True)
class DailyBar:
    open: float | None
    close: float | None
    volume: int | None


@dataclass
class DailySeries:
    bars: dict
    splits: dict
    dividends: dict
    sha256: str

    def factor(self, session):
        return prod(ratio for day, ratio in self.splits.items() if day > session)

    def raw_price(self, session, field):
        bar = self.bars.get(session)
        value = getattr(bar, field) if bar else None
        return round(value * self.factor(session), 6) if value is not None and value > 0 else None

    def traded(self, session, minimum_volume=1):
        bar = self.bars.get(session)
        return bool(bar and bar.volume is not None and bar.volume >= minimum_volume)

    def dividends_per_entry_share(self, entry, exit_day):
        return sum(value * self.factor(entry) for day, value in self.dividends.items()
                   if entry < day <= exit_day)


def read_daily(path: Path, code: str):
    raw = path.read_bytes()
    chart = json.loads(raw)["chart"]
    if chart["error"] or not chart["result"]:
        raise ValueError(f"{path}: {chart['error']}")
    data = chart["result"][0]
    if data["meta"]["symbol"] != f"{code}.T" or data["meta"]["currency"] != "JPY":
        raise ValueError(f"Unexpected symbol or currency in {path}")
    quote = data["indicators"]["quote"][0]
    bars = {epoch_day(t): DailyBar(o, c, v) for t, o, c, v in
            zip(data["timestamp"], quote["open"], quote["close"], quote["volume"], strict=True)}
    events = data.get("events", {})
    splits = {epoch_day(r["date"]): r["numerator"] / r["denominator"]
              for r in events.get("splits", {}).values()}
    dividends = {epoch_day(r["date"]): r["amount"] for r in events.get("dividends", {}).values()}
    return DailySeries(bars, splits, dividends, hashlib.sha256(raw).hexdigest())


@dataclass(frozen=True)
class FiveMinuteBar:
    available_at: datetime
    status: str
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    volume: int | None


def read_intraday(path):
    bars = defaultdict(dict)
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            code = row["code"]
            end = timestamp(row["end_jst"])
            available = timestamp(row["available_at_jst"])
            status = row["status"]
            if (code not in (*STOCKS, ETF) or end.minute % 5 or end.second or end.microsecond
                    or available < end or status not in {"trade", "no_trade", "missing"}
                    or end in bars[code]):
                raise ValueError(f"Invalid or duplicate bar: {code} {end}")
            prices = tuple(float(row[key]) if row[key] else None
                           for key in ("open", "high", "low", "close"))
            volume = int(row["volume"]) if row["volume"] else None
            o, h, low, c = prices
            if status == "trade":
                if not (all(x is not None and isfinite(x) and x > 0 for x in prices)
                        and volume and volume > 0 and low <= min(o, c) <= max(o, c) <= h):
                    raise ValueError(f"Invalid trade bar: {code} {end}")
            elif any(x is not None for x in prices) or volume != (0 if status == "no_trade" else None):
                raise ValueError(f"Invalid empty bar: {code} {end}")
            bars[code][end] = FiveMinuteBar(available, status, *prices, volume)
    return bars


def snapshot(bars, day, observation, observed_by):
    """Bar timestamps are interval ends. A missing slot cannot stand for no trades."""
    end = at(day, observation)
    cursor = at(day, time(9)) + FIVE_MINUTES
    slots = []
    while cursor <= end:
        bar = bars.get(cursor)
        if not bar or bar.available_at > observed_by or bar.status == "missing":
            return "missing", None, []
        slots.append(bar)
        cursor += FIVE_MINUTES
    volume = sum(bar.volume for bar in slots)
    trades = [bar for bar in slots if bar.status == "trade"]
    if not trades:
        return "no_trade", volume, trades
    status = "fresh" if slots[-1].status == "trade" else "stale"
    return status, volume, trades


def previous_close_on_current_basis(series, previous, day):
    if previous is None:
        return None
    close = series.raw_price(previous, "close")
    return close * series.factor(day) / series.factor(previous) if close is not None else None


def pct(value, reference):
    return 100 * (value / reference - 1)


def morning_features(code, sessions, index, data, intraday):
    day = sessions[index]
    previous = sessions[index - 1] if index else None
    cutoff = at(day, DECISION)
    series = data[code]
    etf = data[ETF]
    prev_close = previous_close_on_current_basis(series, previous, day)
    etf_prev = previous_close_on_current_basis(etf, previous, day)
    bars = intraday.get(code, {})
    for obs in OBSERVATIONS:
        status, volume, trades = snapshot(bars, day, obs, cutoff)
        etf_status, _, etf_trades = snapshot(intraday.get(ETF, {}), day, obs, cutoff)
        history = []
        if index >= 25:
            for past in sessions[index - 25:index]:
                _, historical_volume, _ = snapshot(bars, past, obs, cutoff)
                if historical_volume is not None:
                    history.append(historical_volume * series.factor(past) / series.factor(day))
        base_volume = median(history) if len(history) == 25 else None
        quote = trades[-1].close if trades else None
        change = pct(quote, prev_close) if quote is not None and prev_close else None
        etf_change = pct(etf_trades[-1].close, etf_prev) if etf_status == "fresh" and etf_prev else None
        yield {
            "observation_jst": obs.isoformat(timespec="minutes"),
            "stock_status": status, "etf_status": etf_status,
            "previous_close_jpy": prev_close, "etf_previous_close_jpy": etf_prev,
            "last_price_jpy": quote,
            "prev_close_pct": change,
            "open_pct": pct(quote, trades[0].open) if trades else None,
            "high_pct": pct(quote, max(bar.high for bar in trades)) if trades else None,
            "etf_prev_close_pct": etf_change,
            "relative_pp": change - etf_change if change is not None and etf_change is not None else None,
            "cum_volume": volume, "history_sessions": len(history),
            "median_cum_volume_25": base_volume,
            "volume_multiple": volume / base_volume if volume is not None and base_volume else None,
        }


def read_sessions(path):
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return sorted({date.fromisoformat(row["session"]) for row in csv.DictReader(handle)})
