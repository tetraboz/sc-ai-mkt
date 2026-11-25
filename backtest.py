"""Event study of four TSE stocks. Run with Python 3.11+."""
import argparse
import csv
import hashlib
import json
from bisect import bisect_left
from calendar import monthrange
from collections import Counter, defaultdict
from datetime import date, time
from pathlib import Path
from statistics import mean, median

from fundamentals import comparisons, read_facts, signal
from market import (DECISION, ETF, STOCKS, at, morning_features, read_daily,
                    read_intraday, read_sessions)

HORIZONS = (1, 3, 6)
SHARES = 100
BUDGET = 120_000
COST = 0.002
BASE = "fundamentals"
PLUS = "fundamentals_and_morning"


def write_csv(path, rows):
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", newline="", encoding="utf-8") as handle:
        if fields:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)


def month_later(day, months):
    year, month0 = divmod(day.year * 12 + day.month - 1 + months, 12)
    month = month0 + 1
    return date(year, month, min(day.day, monthrange(year, month)[1]))


def exit_info(series, entry, close_day, price, minimum_volume=SHARES):
    if not series.traded(close_day, minimum_volume) or not price:
        return None, None, None
    close = series.raw_price(close_day, "close")
    if close is None:
        return None, None, None
    factor = series.factor(entry) / series.factor(close_day)
    dividends = series.dividends_per_entry_share(entry, close_day)
    return close, dividends, 100 * ((close * factor * (1 - COST) + dividends) / (price * (1 + COST)) - 1)


def morning_signal(rows):
    fields = ("prev_close_pct", "open_pct", "high_pct", "relative_pp", "volume_multiple")
    if any(row["stock_status"] != "fresh" or row["etf_status"] != "fresh"
           or any(row[key] is None for key in fields) for row in rows):
        return None
    nine, ten = rows
    return (nine["relative_pp"] >= 0 and ten["relative_pp"] >= 0 and ten["open_pct"] >= 0
            and ten["high_pct"] >= -2 and ten["volume_multiple"] >= 1)


def first_decision(cutoffs, published):
    index = bisect_left(cutoffs, published)
    return index if index < len(cutoffs) else None


def summarize(rows):
    summary = []
    for group in ("all", *STOCKS):
        for strategy in (BASE, PLUS):
            for months in HORIZONS:
                subset = [r for r in rows if r["strategy"] == strategy
                          and r["months"] == months and (group == "all" or r["code"] == group)]
                counts = Counter(r["status"] for r in subset)
                completed = [r for r in subset if r["status"] == "closed"]
                summary.append({
                    "code": group, "strategy": strategy, "months": months,
                    "opportunities": len(subset), "entries": len(completed) + counts["missing_exit"],
                    "closed": len(completed),
                    "missing_exclusions": sum(v for k, v in counts.items() if k.startswith("missing_")),
                    "missing_financial": counts["missing_financial"],
                    "missing_morning": counts["missing_morning"],
                    "missing_entry": counts["missing_entry"], "missing_exit": counts["missing_exit"],
                    "over_budget": counts["over_budget"],
                    "no_signal": counts["no_signal"], "morning_filter": counts["morning_filter"],
                    "mean_net_pct": mean(r["net_pct"] for r in completed) if completed else None,
                    "median_net_pct": median(r["net_pct"] for r in completed) if completed else None,
                    "etf_observed": sum(r["etf_net_pct"] is not None for r in completed),
                    "mean_excess_pp": (mean(r["excess_pp"] for r in completed if r["excess_pp"] is not None)
                                       if any(r["excess_pp"] is not None for r in completed) else None),
                })
    return summary


def paired_summary(rows):
    """Treat skips as cash and omit missing outcomes from both sides."""
    pairs = defaultdict(dict)
    for r in rows:
        if r["morning_eligible"]:
            pairs[r["code"], r["decision_jst"], r["months"]][r["strategy"]] = r
    summary = []
    for months in HORIZONS:
        subset = [pair for key, pair in pairs.items() if key[2] == months]
        usable = [p for p in subset if all(not r["status"].startswith("missing_") for r in p.values())]
        for strategy in (BASE, PLUS):
            trades = [p[strategy]["net_pct"] for p in usable if p[strategy]["status"] == "closed"]
            summary.append({
                "strategy": strategy, "months": months, "common_opportunities": len(subset),
                "common_missing_exclusions": len(subset) - len(usable),
                "closed": len(trades),
                "mean_trade_net_pct": mean(trades) if trades else None,
                "mean_opportunity_net_pct": sum(trades) / len(usable) if usable else None,
            })
    return summary


def evaluate(code, index, sessions, data, base_signal, morning, decisions):
    day = sessions[index]
    series = data[code]
    etf = data[ETF]
    entry = sessions[index + 1] if index + 1 < len(sessions) else None
    price = series.raw_price(entry, "open")
    debit = price * SHARES * (1 + COST) if price is not None else None
    can_fill = debit is not None and series.traded(entry, SHARES)
    for strategy in (BASE, PLUS):
        if strategy == PLUS and morning is None:
            status = "missing_morning"
        elif base_signal is None:
            status = "missing_financial"
        elif not base_signal:
            status = "no_signal"
        elif strategy == PLUS and not morning:
            status = "morning_filter"
        elif not can_fill:
            status = "missing_entry"
        elif debit > BUDGET:
            status = "over_budget"
        else:
            status = "filled"

        for months in HORIZONS:
            result = {
                "code": code, "decision_jst": at(day, DECISION).isoformat(),
                "strategy": strategy, "months": months, "status": status,
                "morning_eligible": morning is not None, **decisions,
                "reference_entry_day": entry, "reference_open_jpy": price,
                "entry_jpy": None, "cash_debit_jpy": None,
                "exit_day": None, "exit_jpy": None, "dividend_jpy_per_entry_share": None,
                "net_pct": None, "etf_net_pct": None, "excess_pp": None,
            }
            if status == "filled":
                j = bisect_left(sessions, month_later(entry, months))
                exit_day = sessions[j] if j < len(sessions) else None
                close, dividends, net = exit_info(series, entry, exit_day, price)
                etf_price = etf.raw_price(entry, "open") if etf.traded(entry) else None
                _, _, benchmark = exit_info(etf, entry, exit_day, etf_price, minimum_volume=1)
                result.update(status="closed" if net is not None else "missing_exit",
                              entry_jpy=price, cash_debit_jpy=round(debit, 2),
                              exit_day=exit_day, exit_jpy=close, dividend_jpy_per_entry_share=dividends,
                              net_pct=net, etf_net_pct=benchmark,
                              excess_pp=net - benchmark if net is not None and benchmark is not None else None)
            yield result


def run(args):
    data = {code: read_daily(args.daily / f"{code}-daily.json", code) for code in (*STOCKS, ETF)}
    calendar = read_sessions(args.calendar) if args.calendar else sorted(data[ETF].bars)
    sessions = [day for day in calendar if day <= args.as_of]
    if not sessions:
        raise ValueError("No exchange sessions at or before --as-of")
    cutoffs = [at(day, DECISION) for day in sessions]
    facts = read_facts(args.facts)
    facts = [f for f in facts if f.timestamp <= cutoffs[-1]]
    signals = defaultdict(list)
    financial_rows = []
    for row in comparisons(facts):
        idx = first_decision(cutoffs, row["available_at_jst"])
        if idx is None or not args.start <= sessions[idx] <= args.end:
            continue
        financial_rows.append({**row, "decision_jst": cutoffs[idx]})
        signals[row["code"], idx].append(row)

    intraday = read_intraday(args.intraday) if args.intraday else {}
    morning_rows = []
    results = []
    for (code, idx), rows in sorted(signals.items(), key=lambda pair: (pair[0][1], pair[0][0])):
        base, reasons = signal(rows)
        observations = list(morning_features(code, sessions, idx, data, intraday))
        morning = morning_signal(observations)
        decision = cutoffs[idx].isoformat()
        morning_rows.extend({"code": code, "decision_jst": decision, **row} for row in observations)
        results.extend(evaluate(code, idx, sessions, data, base, morning,
                                {"financial_signal": base, "financial_reasons": reasons,
                                 "morning_signal": morning}))
    manifest = {
        "start_decision_jst": args.start.isoformat(), "end_decision_jst": args.end.isoformat(),
        "as_of_jst": args.as_of.isoformat(), "decision_time_jst": "10:30",
        "shares": SHARES, "max_buy_debit_jpy": BUDGET, "cost_per_side": COST,
        "calendar": str(args.calendar) if args.calendar else "200A daily timestamps",
        "eligible_decisions": len(signals),
        "morning_comparison": "evaluated_on_available_rows"
        if any(row["morning_eligible"] and row["financial_signal"] is not None for row in results)
        else "not_evaluated",
        "market_surprise_complete_rows": sum(r["comparison"] == "market_surprise"
                                             and r["eps_delta_jpy"] is not None
                                             and r["op_delta_mjpy"] is not None for r in financial_rows
                                             if "eps_delta_jpy" in r),
        "daily_sha256": {code: series.sha256 for code, series in data.items()},
        "fundamentals_sha256": hashlib.sha256(args.facts.read_bytes()).hexdigest(),
        "intraday_sha256": hashlib.sha256(args.intraday.read_bytes()).hexdigest() if args.intraday else None,
    }
    args.out.mkdir(exist_ok=True, parents=True)
    for name, rows in (("fundamentals", financial_rows), ("morning", morning_rows), ("returns", results),
                       ("summary", summarize(results)), ("paired_summary", paired_summary(results))):
        write_csv(args.out / f"{name}.csv", rows)
    (args.out / "run.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--daily", type=Path, default=Path("data/raw"))
    parser.add_argument("--facts", type=Path, default=Path("data/fundamentals.csv"))
    parser.add_argument("--intraday", type=Path, help="point-in-time five minute CSV")
    parser.add_argument("--calendar", type=Path, help="exchange session CSV, column: session")
    parser.add_argument("--start", type=date.fromisoformat, default=date(2025, 11, 1))
    parser.add_argument("--end", type=date.fromisoformat, default=date(2026, 5, 31))
    parser.add_argument("--as-of", type=date.fromisoformat, default=date(2026, 11, 24))
    parser.add_argument("--out", type=Path, default=Path("results"))
    args = parser.parse_args()
    if not args.start <= args.end <= args.as_of:
        parser.error("need start <= end <= as-of")
    run(args)


if __name__ == "__main__":
    main()
