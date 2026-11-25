"""Download the daily Yahoo Finance chart snapshots used by the example."""
import argparse
import json
from datetime import date, datetime, time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

CODES = ("5381", "5542", "5367", "3446", "200A")
JST = ZoneInfo("Asia/Tokyo")


def daily_url(code, start, stop):
    def epoch(day):
        return int(datetime.combine(day, time(9), JST).timestamp())

    query = urlencode(dict(period1=epoch(start), period2=epoch(stop),
                           interval="1d", events="div,splits"))
    return f"https://query2.finance.yahoo.com/v8/finance/chart/{code}.T?{query}"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("data/raw"))
    parser.add_argument("--start", type=date.fromisoformat, default=date(2024, 12, 1))
    parser.add_argument("--stop", type=date.fromisoformat, default=date(2026, 11, 25),
                        help="exclusive date")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    for code in CODES:
        url = daily_url(code, args.start, args.stop)
        with urlopen(Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=30) as response:
            raw = response.read()
        payload = json.loads(raw)["chart"]
        if payload["error"] or not payload["result"]:
            raise RuntimeError(f"{code}: {payload['error']}")
        (args.out / f"{code}-daily.json").write_bytes(raw)
        print(code, url)


if __name__ == "__main__":
    main()
