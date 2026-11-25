import csv
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import date, time, timedelta
from pathlib import Path

from market import ETF, STOCKS, at


class PortfolioExperiment(unittest.TestCase):
    """Exercise the CLI with a known price path; all quotes and facts are synthetic."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.days = [
            date(2025, 1, 1) + timedelta(days=n)
            for n in range(330)
            if (date(2025, 1, 1) + timedelta(days=n)).weekday() < 5
        ]
        self.candidate = date(2025, 3, 3)
        self.i = self.days.index(self.candidate)
        self.config = json.loads(Path("strategy.json").read_text())
        self.config.update(
            development_start="2025-02-01",
            evaluation_start="2025-03-01",
            last_candidate="2025-03-10",
            as_of="2025-10-01",
            initial_cash_jpy=100000,
        )
        self.facts = []
        self.add_forecast("5367", self.days[self.i - 1], 9, -10, 100)
        self.add_forecast("5367", self.candidate, 9, -5, 110)
        self.calendar = self.root / "calendar.csv"
        self.calendar.write_text("session\n" + "\n".join(map(str, self.days)) + "\n")
        for code in (*STOCKS, ETF):
            opens = [500] * len(self.days)
            opens[self.i + 2] = 400
            quote = dict(
                open=opens, close=[500] * len(opens), volume=[10000] * len(opens)
            )
            data = dict(
                meta=dict(symbol=code + ".T", currency="JPY"),
                indicators=dict(quote=[quote]),
                timestamp=[int(at(d, time(9)).timestamp()) for d in self.days],
            )
            (self.root / f"{code}-daily.json").write_text(
                json.dumps(dict(chart=dict(result=[data], error=None)))
            )

    def add_forecast(self, code, day, hour, eps, op):
        fields = Path("data/fundamentals.csv").read_text().splitlines()[0].split(",")
        row = dict.fromkeys(fields, "")
        row.update(
            code=code,
            available_at_jst=at(day, time(hour)).isoformat(),
            period_start="2025-01-01",
            period_end="2025-12-31",
            kind="forecast",
            scope="group",
            eps_basis="basic",
            eps_jpy=eps,
            op_mjpy=op,
            source="synthetic",
        )
        self.facts.append(row)

    def execute(self, late=False, always_wait=False):
        with (self.root / "facts.csv").open("w") as f:
            w = csv.DictWriter(f, fieldnames=self.facts[0])
            w.writeheader()
            w.writerows(self.facts)
        (self.root / "config.json").write_text(json.dumps(self.config))
        with (self.root / "bars.csv").open("w") as f:
            w = csv.writer(f)
            w.writerow(
                "code end_jst available_at_jst status open high low close volume".split()
            )
            for day in self.days[self.i - 25 : self.i + 5]:
                for code in (ETF, "5367", "5381"):
                    price = (
                        490
                        if code != ETF and (day == self.candidate or always_wait)
                        else 500
                    )
                    for k in range(1, 13):
                        end = at(day, time(9)) + timedelta(minutes=5 * k)
                        delayed = (
                            late and day == self.candidate and k == 6 and code == "5367"
                        )
                        w.writerow(
                            (
                                code,
                                end,
                                at(day, time(10, 5)) if delayed else end,
                                "trade",
                                price,
                                price,
                                price,
                                price,
                                100,
                            )
                        )
        out = self.root / "out"
        subprocess.run(
            [
                sys.executable,
                "backtest.py",
                "--config",
                str(self.root / "config.json"),
                "--facts",
                str(self.root / "facts.csv"),
                "--calendar",
                str(self.calendar),
                "--daily",
                str(self.root),
                "--intraday",
                str(self.root / "bars.csv"),
                "--out",
                str(out),
            ],
            check=True,
            capture_output=True,
        )

        def load(name):
            with (out / (name + ".csv")).open() as f:
                return list(csv.DictReader(f))

        return load

    def test_delayed_buy_same_exit_and_cash_attribution(self):
        read = self.execute()
        rows = [r for r in read("returns") if r["months"] == "3"]
        a, b = rows
        self.assertEqual((a["exit"], b["exit"]), ("2025-06-03",) * 2)
        self.assertEqual((a["entry"], b["entry"]), ("2025-03-04", "2025-03-05"))
        pair = next(r for r in read("pairs") if r["months"] == "3")
        self.assertAlmostEqual(float(pair["delta_jpy"]), 10020)
        nav = [
            r
            for r in read("equity")
            if r["months"] == "3" and r["phase"] == "evaluation"
        ]
        self.assertEqual(
            float(
                next(
                    r["cash"]
                    for r in nav
                    if r["day"] == a["entry"] and r["strategy"] == "B"
                )
            ),
            100000,
        )
        self.assertAlmostEqual(float(nav[-1]["equity"]), 109820)

    def test_late_quote_is_unknown_not_cash(self):
        read = self.execute(late=True)
        pair = next(r for r in read("pairs") if r["months"] == "3")
        self.assertEqual((pair["B_status"], pair["delta_jpy"]), ("missing_morning", ""))

    def test_expiry_and_supersession(self):
        read = self.execute(always_wait=True)
        b = next(
            r for r in read("returns") if r["months"] == "3" and r["strategy"] == "B"
        )
        self.assertEqual((b["status"], float(b["pnl"])), ("expired", 0))
        self.add_forecast("5367", self.days[self.i + 1], 9, -15, 90)
        read = self.execute()
        self.assertEqual(
            next(r["B_status"] for r in read("pairs") if r["months"] == "3"),
            "invalidated",
        )

    def test_capital_and_purge(self):
        self.add_forecast("5381", self.days[self.i - 1], 9, -10, 100)
        self.add_forecast("5381", self.candidate, 9, -5, 110)
        self.add_forecast("3446", date(2025, 2, 4), 9, 1, 5)
        self.add_forecast("3446", date(2025, 2, 5), 9, 2, 6)
        read = self.execute()
        a = [r for r in read("returns") if r["months"] == "3" and r["strategy"] == "A"]
        self.assertEqual({r["status"] for r in a}, {"sold", "insufficient_cash"})
        self.assertTrue(
            any(
                r["id"] == "2025-02-05-3446" and r["months"] == "3"
                for r in read("purged")
            )
        )

    def test_latest_forecast_for_same_decision(self):
        self.add_forecast("5367", self.candidate, 10, -15, 90)
        read = self.execute()
        self.assertFalse(read("returns"))
        self.assertEqual(read("events")[-1]["candidate"], "False")


if __name__ == "__main__":
    unittest.main()
