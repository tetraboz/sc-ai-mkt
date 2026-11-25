"""Compare dated facts on the same reporting and per-share basis."""
import csv
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from market import STOCKS, timestamp

EPS_NEAR_ZERO = Decimal("1.00")


@dataclass(frozen=True)
class Fact:
    code: str
    timestamp: datetime
    kind: str
    start: date
    end: date
    scope: str
    eps_basis: str
    eps: Decimal | None
    op: Decimal | None
    order_metric: str
    definition: str
    amount: Decimal | None
    compare_start: date | None
    compare_end: date | None
    source: str

    @property
    def period(self):
        return self.code, self.start, self.end, self.scope

    @property
    def order_key(self):
        return self.code, self.scope, self.order_metric, self.definition


def read_facts(path):
    def number(row, field):
        if not row[field]:
            return None
        value = Decimal(row[field])
        if not value.is_finite():
            raise ValueError(f"Non-finite {field}: {row['source']}")
        return value

    def day(row, field):
        return date.fromisoformat(row[field]) if row[field] else None

    facts = []
    keys = set()
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            fact = Fact(row["code"], timestamp(row["available_at_jst"]), row["kind"],
                        day(row, "period_start"), day(row, "period_end"), row["scope"],
                        row["eps_basis"], number(row, "eps_jpy"), number(row, "op_mjpy"),
                        row["order_metric"], row["order_definition"], number(row, "order_mjpy"),
                        day(row, "compare_start"), day(row, "compare_end"), row["source"])
            key = (fact.period, fact.timestamp, fact.kind, fact.order_key)
            if (not fact.start or not fact.end or fact.end < fact.start or not fact.scope
                    or not fact.source or fact.code not in STOCKS or key in keys):
                raise ValueError(f"Invalid or duplicate fact: {fact}")
            if fact.kind == "order":
                if (fact.order_metric not in {"orders_received", "order_backlog"}
                        or not fact.definition or fact.amount is None or fact.amount < 0
                        or bool(fact.compare_start) != bool(fact.compare_end)):
                    raise ValueError(f"Invalid order series: {fact}")
                if fact.compare_end:
                    spans = ((fact.end - fact.start).days, (fact.compare_end - fact.compare_start).days)
                    if (fact.compare_end >= fact.end or abs(spans[0] - spans[1]) > 1
                            or (fact.start.month, fact.start.day) !=
                            (fact.compare_start.month, fact.compare_start.day)
                            or (fact.end.month, fact.end.day) !=
                            (fact.compare_end.month, fact.compare_end.day)):
                        raise ValueError(f"Order comparison periods must match: {fact}")
            elif fact.kind not in {"forecast", "consensus", "actual"} or (fact.eps is not None and not fact.eps_basis):
                raise ValueError(f"Invalid financial fact: {fact}")
            facts.append(fact)
            keys.add(key)
    return sorted(facts, key=lambda fact: fact.timestamp)


def diff(current, prior):
    return current - prior if current is not None and prior is not None else None


def sign(value):
    if value is None:
        return None
    return "profit" if value > 0 else "loss" if value < 0 else "zero"


def transition(current, prior):
    return f"{sign(prior)}_to_{sign(current)}" if current is not None and prior is not None else None


def financial_comparison(fact, prior, comparison):
    previous = prior.eps if prior and fact.eps_basis == prior.eps_basis else None
    previous_op = prior.op if prior else None
    eps_delta = diff(fact.eps, previous)
    op_delta = diff(fact.op, previous_op)
    eps_rate = (100 * eps_delta / previous
                if comparison == "forecast_revision" and eps_delta is not None
                and min(previous, fact.eps) > EPS_NEAR_ZERO else None)
    return {
        "comparison": comparison,
        "eps_jpy": fact.eps, "prior_eps_jpy": previous, "eps_delta_jpy": eps_delta,
        "eps_revision_pct": eps_rate,
        "eps_revision_mode": ("pct" if eps_rate is not None else "delta")
        if comparison == "forecast_revision" and eps_delta is not None else None,
        "eps_sign_change": transition(fact.eps, previous),
        "op_mjpy": fact.op, "prior_op_mjpy": previous_op, "op_delta_mjpy": op_delta,
        "op_sign_change": transition(fact.op, previous_op),
    }


def comparisons(facts):
    """Only use prior snapshots, even when a newer forecast shares an announcement."""
    series = defaultdict(list)
    for fact in facts:
        if fact.kind == "consensus":
            series[fact.kind, fact.period].append(fact)
            continue
        if fact.kind == "order":
            if fact.compare_end is None:
                series["order", (fact.order_key, fact.start, fact.end)].append(fact)
                continue
            targets = [("order_yoy", "order", (fact.order_key, fact.compare_start, fact.compare_end))]
        else:
            targets = [("forecast_revision" if fact.kind == "forecast" else "actual_vs_company",
                        "forecast", fact.period)]
            if fact.kind == "actual":
                targets.append(("market_surprise", "consensus", fact.period))
        for label, kind, key in targets:
            # Comparable prior-year order values may appear for the first time in this same release.
            candidates = [p for p in series[kind, key]
                          if p.timestamp < fact.timestamp or (kind == "order" and p.timestamp == fact.timestamp)]
            # A revision to an already disclosed actual uses the information available before the first release.
            first_actual = series["actual", fact.period]
            if fact.kind == "actual" and first_actual:
                candidates = [p for p in candidates if p.timestamp < first_actual[0].timestamp]
            prior = candidates[-1] if candidates else None
            if label == "order_yoy":
                prior_amount = prior.amount if prior else None
                change = diff(fact.amount, prior_amount)
                values = {
                    "comparison": label, "order_metric": fact.order_metric,
                    "order_definition": fact.definition, "order_mjpy": fact.amount,
                    "compare_start": fact.compare_start, "compare_end": fact.compare_end,
                    "prior_order_mjpy": prior_amount, "order_delta_mjpy": change,
                    "order_yoy_pct": 100 * change / prior_amount if prior_amount else None,
                }
            else:
                values = financial_comparison(fact, prior, label)
            yield {
                "code": fact.code, "available_at_jst": fact.timestamp,
                "period_start": fact.start, "period_end": fact.end, "scope": fact.scope,
                "eps_basis": fact.eps_basis, "source": fact.source,
                "prior_available_at_jst": prior.timestamp if prior else None,
                "prior_source": prior.source if prior else None,
                **values,
            }
        key = (fact.order_key, fact.start, fact.end) if fact.kind == "order" else fact.period
        series[fact.kind, key].append(fact)


def signal(rows):
    complete = [
        row for row in rows
        if row.get("eps_delta_jpy") is not None and row.get("op_delta_mjpy") is not None
    ]
    reasons = sorted({row["comparison"] for row in complete
                      if row["eps_delta_jpy"] > 0 and row["op_delta_mjpy"] > 0})
    return bool(reasons) if complete else None, ",".join(reasons)
