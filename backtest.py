"""Event study of four TSE stocks. Run with Python 3.11+."""

import argparse
import csv
import hashlib
import json
from bisect import bisect_left
from calendar import monthrange
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from portfolio import simulate

from fundamentals import comparisons, read_facts, signal
from market import (
    DECISION,
    ETF,
    STOCKS,
    at,
    morning_features,
    read_daily,
    read_intraday,
    read_sessions,
)


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


def morning_decision(rows, cfg):
    reasons = {
        r[k] for r in rows for k in ("stock_status", "etf_status") if r[k] != "fresh"
    }
    required = ("relative_pp", "open_pct", "high_pct", "volume_multiple")
    if "missing" in reasons or any(r["history_sessions"] < 25 for r in rows):
        return "missing"
    if reasons:
        return "+".join(sorted(reasons))
    if any(r[k] is None for r in rows for k in required):
        return "missing"
    nine, ten = rows
    passed = (
        min(nine["relative_pp"], ten["relative_pp"]) >= cfg["min_relative_pp_both"]
        and ten["open_pct"] >= cfg["min_open_pct_1000"]
        and ten["high_pct"] >= cfg["min_high_pct_1000"]
        and ten["volume_multiple"] >= cfg["min_volume_multiple_1000"]
    )
    return "buy" if passed else "wait"


def plan(code, idx, sessions, events, data, bars, cfg, asof, observations):
    first = None
    for j in range(idx, min(idx + cfg["max_decision_sessions"], len(sessions))):
        day = sessions[j]
        if day > asof:
            break
        if j != idx and cfg["expire_on_next_financial_event"] and (code, j) in events:
            return dict(first=first, status="invalidated")
        rows = list(morning_features(code, sessions, j, data, bars))
        decision = morning_decision(rows, cfg)
        observations.extend(
            dict(candidate=sessions[idx], day=day, code=code, decision=decision, **r)
            for r in rows
        )
        first = first or decision
        if decision == "missing":
            return dict(first=first, status="missing_morning", unknown_day=day)
        if decision == "buy":
            return dict(
                first=first,
                status="order",
                signal_day=day,
                entry=sessions[j + 1] if j + 1 < len(sessions) else None,
            )
        if j - idx + 1 == cfg["max_decision_sessions"]:
            return dict(first=first, status="expired")
    return dict(first=first, status="right_censored")


def profile(events):
    """Count connected calendar intervals, not independent observations."""
    groups = 0
    end = None
    for e in sorted(events, key=lambda e: e["day"]):
        if end is None or e["day"] > end:
            groups += 1
        end = max(end or e["day"], e["target"] or date.max)
    return groups


def run(args):
    cfg = json.loads(args.config.read_text())
    start, split, end, asof = (
        date.fromisoformat(cfg[k])
        for k in ("development_start", "evaluation_start", "last_candidate", "as_of")
    )
    if (
        not start < split <= end <= asof
        or min(
            cfg["max_decision_sessions"],
            cfg["shares"],
            cfg["initial_cash_jpy"],
            cfg["order_limit_jpy"],
        )
        <= 0
        or not 0 <= cfg["cost_per_side"] < 1
    ):
        raise ValueError("Invalid date, cost, capital or waiting window")
    sessions = read_sessions(args.calendar)
    if not sessions or sessions[0] > start or sessions[-1] < asof:
        raise ValueError("Exchange calendar must cover the full observation period")
    cutoffs = [at(day, DECISION) for day in sessions]
    data = {c: read_daily(args.daily / f"{c}-daily.json", c) for c in (*STOCKS, ETF)}
    bars = read_intraday(args.intraday) if args.intraday else {}
    events = defaultdict(list)
    financial = []
    for row in comparisons(read_facts(args.facts)):
        idx = bisect_left(cutoffs, row["available_at_jst"])
        if idx >= len(sessions) or not start <= sessions[idx] <= asof:
            continue
        financial.append(dict(decision_jst=cutoffs[idx], **row))
        if row["comparison"] != "order_yoy":
            events[row["code"], idx].append(row)
    candidates, observations, decisions = [], [], []
    for (code, idx), rows in sorted(
        events.items(), key=lambda item: (item[0][1], item[0][0])
    ):
        day = sessions[idx]
        if day > end:
            continue
        buy, reason = signal(rows)
        decisions.append(
            dict(
                code=code,
                day=day,
                candidate=buy,
                reason=reason,
                releases=len({r["available_at_jst"] for r in rows}),
            )
        )
        if buy:
            candidates.append(
                dict(
                    id=f"{day}-{code}",
                    code=code,
                    day=day,
                    A=dict(
                        first="buy",
                        status="order",
                        signal_day=day,
                        entry=sessions[idx + 1] if idx + 1 < len(sessions) else None,
                    ),
                    B=plan(
                        code, idx, sessions, events, data, bars, cfg, asof, observations
                    ),
                )
            )
    all_trades, curves, summaries, pairs, attribution, exclusions = (
        [],
        [],
        [],
        [],
        [],
        [],
    )
    for months in [cfg["primary_months"], *cfg["secondary_months"]]:
        for phase in ("development", "evaluation"):
            selected = []
            for c in candidates:
                if (phase == "development") != (c["day"] < split):
                    continue
                j = bisect_left(sessions, month_later(c["day"], months))
                target = sessions[j] if j < len(sessions) else None
                if phase == "development" and (target is None or target >= split):
                    exclusions.append(
                        dict(
                            id=c["id"],
                            months=months,
                            reason="purged_overlap",
                            target=target,
                        )
                    )
                    continue
                event = dict(c, target=target)
                # A pending candidate expires before an order could first fill after liquidation.
                for strategy in ("A", "B"):
                    p = event[strategy]
                    if target and (
                        p.get("entry", None)
                        and p["entry"] > target
                        or p.get("unknown_day", None)
                        and p["unknown_day"] >= target
                    ):
                        event[strategy] = dict(first=p["first"], status="expired")
                selected.append(event)
            lo = start if phase == "development" else split
            last_signal = split - timedelta(days=1) if phase == "development" else end
            hi = min(asof, max([last_signal, *(c["target"] or asof for c in selected)]))
            days = [d for d in sessions if lo <= d <= hi]
            books = {}
            terminal = {}
            for strategy in ("A", "B"):
                unknown, orders = defaultdict(list), defaultdict(list)
                for c in selected:
                    p = c[strategy]
                    if p.get("unknown_day"):
                        unknown[p["unknown_day"]].append(c["id"])
                    if p.get("entry") and (
                        not c["target"] or p["entry"] <= c["target"]
                    ):
                        orders[p["entry"]].append(c["id"])
                trades, curve, summary = simulate(
                    phase, strategy, months, days, selected, data, cfg, unknown, orders
                )
                all_trades.extend(trades)
                curves.extend(curve)
                summaries.append(summary)
                terminal[strategy] = summary["final_equity"]
                books[strategy] = {t["id"]: t for t in trades}
            subset = []
            for c in selected:
                a, b = (books[k][c["id"]] for k in ("A", "B"))
                mature = c["target"] is not None and c["target"] <= asof
                delta = (
                    b["pnl"] - a["pnl"]
                    if mature and None not in (a["pnl"], b["pnl"])
                    else None
                )
                diff = (
                    None
                    if b["decision"] in ("missing", None)
                    else b["decision"] != "buy"
                )
                row = dict(
                    phase=phase,
                    months=months,
                    id=c["id"],
                    target=c["target"],
                    different_first_decision=diff,
                    A_status=a["status"],
                    B_status=b["status"],
                    A_pnl=a["pnl"],
                    B_pnl=b["pnl"],
                    delta_jpy=delta,
                )
                pairs.append(row)
                subset.append(row)
            known = [r for r in subset if r["delta_jpy"] is not None]
            differing = [r["delta_jpy"] for r in known if r["different_first_decision"]]
            # A cost or signal rejection by both strategies yields no observed purchase outcome.
            bought = any("sold" in (r["A_status"], r["B_status"]) for r in known)
            nav_diff = (
                terminal["B"] - terminal["A"]
                if None not in terminal.values()
                and bought
                and len(known) == len(selected)
                else None
            )
            attribution.append(
                dict(
                    portfolio_delta_jpy=nav_diff,
                    portfolio_return_difference_pp=100
                    * nav_diff
                    / cfg["initial_cash_jpy"]
                    if nav_diff is not None
                    else None,
                    phase=phase,
                    months=months,
                    candidates=len(selected),
                    overlap_groups=profile(selected),
                    common_completed=len(known),
                    different_decisions_observed=sum(
                        r["different_first_decision"] is True for r in subset
                    ),
                    unknown_decisions=sum(
                        r["different_first_decision"] is None for r in subset
                    ),
                    differing_with_outcome=len(differing),
                    improvement_jpy=sum(max(v, 0) for v in differing)
                    if known
                    else None,
                    deterioration_jpy=sum(min(v, 0) for v in differing)
                    if known
                    else None,
                    total_paired_delta_jpy=sum(r["delta_jpy"] for r in known)
                    if known
                    else None,
                    status=("evaluated" if len(known) == len(selected) else "partial")
                    if bought
                    else "not_evaluated",
                )
            )
    args.out.mkdir(parents=True, exist_ok=True)
    for name, rows in dict(
        fundamentals=financial,
        events=decisions,
        morning=observations,
        returns=all_trades,
        equity=curves,
        summary=summaries,
        pairs=pairs,
        paired_summary=attribution,
        purged=exclusions,
    ).items():
        write_csv(args.out / f"{name}.csv", rows)
    manifest = dict(
        config=cfg,
        config_sha256=digest(args.config),
        calendar_sha256=digest(args.calendar),
        rules_tried=1,
        threshold_searches=0,
        financial_event_days=len(decisions),
        earnings_announcements=len(
            {
                (r["code"], r["available_at_jst"])
                for r in financial
                if r["comparison"] == "actual_vs_company"
                and r["decision_jst"].date() <= end
            }
        ),
        candidate_events=len(candidates),
        facts_sha256=digest(args.facts),
        daily_sha256={k: v.sha256 for k, v in data.items()},
        intraday_sha256=digest(args.intraday) if args.intraday else None,
        primary_evaluation=next(
            r
            for r in attribution
            if r["phase"] == "evaluation" and r["months"] == cfg["primary_months"]
        ),
    )
    (args.out / "run.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest["primary_evaluation"], indent=2))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("strategy.json"))
    parser.add_argument("--daily", type=Path, default=Path("data/raw"))
    parser.add_argument("--facts", type=Path, default=Path("data/fundamentals.csv"))
    parser.add_argument("--intraday", type=Path)
    parser.add_argument("--calendar", type=Path, default=Path("data/sessions.csv"))
    parser.add_argument("--out", type=Path, default=Path("results"))
    run(parser.parse_args())
