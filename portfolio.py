"""Daily self-financing long-only portfolio with dated instructions and an opening price cap."""

from statistics import mean

from market import ETF


def simulate(phase, strategy, months, days, candidates, data, cfg, unknown, orders):
    """Only the day's scheduled actions are visible to this cash book."""
    cash = initial = cfg["initial_cash_jpy"]
    fee, size = cfg["cost_per_side"], cfg["shares"]
    accrued = 0.0  # Dividend entitlement; payment dates unavailable, never reinvest.
    positions = {}
    records = {}
    curve = []
    uncertain = None
    for event in candidates:
        key = event["id"]
        plan = event[strategy]
        records[key] = dict(
            id=key,
            code=event["code"],
            candidate=event["day"],
            target=event["target"],
            phase=phase,
            months=months,
            strategy=strategy,
            decision=plan["first"],
            order_signal_day=plan.get("signal_day"),
            status=plan["status"],
            entry=None,
            entry_price=None,
            debit=None,
            exit=None,
            exit_price=None,
            income=0.0,
            etf_net_pct=None,
            pnl=None,
            net_pct=None,
        )
    for day in days:
        if uncertain is None:
            # Existing holders earn today's distribution, including on the sale date.
            for key, units in positions.items():
                rec = records[key]
                income = units * data[rec["code"]].dividends.get(day, 0)
                accrued += income
                rec["income"] += income
            for key in sorted(orders.get(day, [])):
                rec = records[key]
                series = data[rec["code"]]
                bar = series.bars.get(day)
                price = series.raw_price(day, "open")
                if (
                    bar
                    and bar.volume is not None
                    and bar.volume / series.factor(day) < size
                ):
                    rec["status"] = "unfilled_volume"
                    continue
                if not bar or bar.volume is None or price is None:
                    rec["status"] = "missing_entry"
                    uncertain = day
                    break
                debit = size * price * (1 + fee)
                if debit > cfg["order_limit_jpy"]:
                    rec["status"] = "over_budget"
                elif debit > cash:
                    rec["status"] = "insufficient_cash"
                else:
                    cash -= debit
                    positions[key] = size * series.factor(day)
                    rec.update(
                        status="holding", entry=day, entry_price=price, debit=debit
                    )
            if unknown.get(day):
                uncertain = day
        if uncertain is None:
            for key, units in list(positions.items()):
                rec = records[key]
                if rec["target"] != day:
                    continue
                series = data[rec["code"]]
                close = series.raw_price(day, "close")
                if close is None or not series.traded(day, units):
                    rec["status"] = "unresolved_exit"
                    uncertain = day
                    break
                proceeds = close * units / series.factor(day) * (1 - fee)
                cash += proceeds
                pnl = proceeds + rec["income"] - rec["debit"]
                etf = data[ETF]
                buy_etf = etf.raw_price(rec["entry"], "open")
                sell_etf = etf.raw_price(day, "close")
                if (
                    buy_etf
                    and sell_etf
                    and etf.traded(rec["entry"])
                    and etf.traded(day)
                ):
                    adjusted_sale = (
                        sell_etf * etf.factor(rec["entry"]) / etf.factor(day)
                    )
                    income_etf = etf.dividends_per_entry_share(rec["entry"], day)
                    rec["etf_net_pct"] = 100 * (
                        (adjusted_sale * (1 - fee) + income_etf) / (buy_etf * (1 + fee))
                        - 1
                    )
                rec.update(
                    status="sold",
                    exit=day,
                    exit_price=close,
                    pnl=pnl,
                    net_pct=100 * pnl / rec["debit"],
                )
                del positions[key]
        equity = None
        if uncertain is None:
            value = 0.0
            for key, units in positions.items():
                bar = data[records[key]["code"]].bars.get(day)
                if not bar or not bar.close:
                    value = None
                    break
                value += units * bar.close
            if value is not None:
                equity = cash + accrued + value
        curve.append(
            dict(
                phase=phase,
                strategy=strategy,
                months=months,
                day=day,
                cash=cash if uncertain is None else None,
                dividend_receivable=accrued if uncertain is None else None,
                equity=equity,
                positions=len(positions) if uncertain is None else None,
                unknown_since=uncertain,
            )
        )
    for rec in records.values():
        if rec["status"] in (
            "expired",
            "invalidated",
            "over_budget",
            "unfilled_volume",
            "insufficient_cash",
        ):
            if (
                uncertain is None
                or rec["candidate"] < uncertain
                and rec["target"]
                and rec["target"] < uncertain
            ):
                rec["pnl"] = 0.0
        if rec["status"] == "order":
            rec["status"] = "unknown_portfolio" if uncertain else "awaiting_open"
    trades = [r["net_pct"] for r in records.values() if r["status"] == "sold"]
    equity = curve[-1]["equity"] if curve else initial
    return (
        list(records.values()),
        curve,
        dict(
            phase=phase,
            strategy=strategy,
            months=months,
            candidates=len(candidates),
            buys=sum(r["entry"] is not None for r in records.values()),
            missing_morning=sum(
                r["status"] == "missing_morning" for r in records.values()
            ),
            expired=sum(r["status"] == "expired" for r in records.values()),
            invalidated=sum(r["status"] == "invalidated" for r in records.values()),
            unfilled=sum(
                r["status"] in ("over_budget", "insufficient_cash", "unfilled_volume")
                for r in records.values()
            ),
            sold=len(trades),
            mean_trade_net_pct=mean(trades) if trades else None,
            final_equity=equity,
            portfolio_net_pct=100 * (equity / initial - 1)
            if equity is not None
            else None,
            unknown_since=uncertain,
        ),
    )
