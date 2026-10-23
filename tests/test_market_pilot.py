"""Small isolated pilot tests; no live API or production database access."""

import csv
from pathlib import Path
import sqlite3
import tempfile
import unittest

from src.market_data.market_data_models import DailyPrice
from src.market_data.pilot import read_manifest, run_pilot, select_manifest
from src.market_data.providers.tiingo import TiingoProvider, UnknownTicker
from src.market_data.validate_pilot import validate


class FakeProvider:
    name = "tiingo"

    def __init__(self):
        self.calls = 0

    def get_metadata(self, symbol):
        self.calls += 1
        return {"ticker": symbol, "name": "AAA Incorporated",
                "startDate": "2019-01-01", "endDate": "2021-01-01"}

    def get_daily_prices(self, symbol, start, end):
        self.calls += 1
        return [DailyPrice(symbol, start, 10, 12, 9, 11, 9, 11, 8, 10,
            1000, self.name, adjusted_volume=1100, split_factor=1,
            dividend_cash=0, adjustment_basis="TIINGO_DIVIDEND_AND_SPLIT_ADJUSTED")]


class RecycledProvider(FakeProvider):
    def get_metadata(self, symbol):
        self.calls += 1
        return {"ticker": symbol, "name": "Unrelated Recycled Issuer",
                "startDate": "2019-01-01", "endDate": "2021-01-01"}


class UnknownProvider(FakeProvider):
    def get_metadata(self, symbol):
        self.calls += 1
        raise UnknownTicker(symbol)


class StubTiingo(TiingoProvider):
    def __init__(self, payload):
        self.payload = payload

    def _fetch(self, symbol, suffix=""):
        return self.payload


class PilotTests(unittest.TestCase):
    def test_tiingo_payload_fields_and_duplicate_rejection(self):
        row = {"date": "2020-01-02T00:00:00+00:00", "open": 10, "high": 12,
               "low": 9, "close": 11, "volume": 1000, "adjOpen": 9,
               "adjHigh": 11, "adjLow": 8, "adjClose": 10,
               "adjVolume": 1100, "splitFactor": 1, "divCash": .1}
        price = StubTiingo([row]).get_daily_prices("AAA", "2020-01-01", "2020-01-03")[0]
        self.assertEqual((price.trading_date, price.adjusted_close, price.dividend_cash),
                         ("2020-01-02", 10, .1))
        with self.assertRaises(ValueError):
            StubTiingo([row, row]).get_daily_prices("AAA", "2020-01-01", "2020-01-03")
        incomplete = dict(row)
        del incomplete["adjClose"]
        with self.assertRaises(ValueError):
            StubTiingo([incomplete]).get_daily_prices("AAA", "2020-01-01", "2020-01-03")

    def test_approval_identity_persistence_and_resume(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as tmp:
            root = Path(tmp)
            manifest = root / "manifest.csv"
            with manifest.open("w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["security_id", "issuer_cik", "ticker",
                    "provider_ticker", "verified_provider_name", "provider_security_id",
                    "valid_from", "valid_to", "review_status",
                    "selection_reason", "mapping_method", "mapping_confidence"])
                writer.writeheader()
                writer.writerow(dict(security_id="m1", issuer_cik="0001", ticker="AAA",
                    provider_ticker="AAA", verified_provider_name="AAA Incorporated",
                    provider_security_id="", valid_from="2019-01-01", valid_to="2021-01-01",
                    review_status="PENDING", selection_reason="period_diversity",
                    mapping_method="OBSERVED_INTERVAL", mapping_confidence="HIGH"))
            with self.assertRaises(ValueError):
                read_manifest(manifest)
            text = manifest.read_text().replace("PENDING", "APPROVED")
            manifest.write_text(text)
            provider = FakeProvider()
            db = root / "pilot.db"
            result = run_pilot(manifest, db, provider, "2020-01-02", "2020-01-03")
            self.assertEqual(result["complete"], 1)
            self.assertEqual(result["api_requests"], 2)
            with self.assertRaises(ValueError):
                run_pilot(manifest, db, provider, "2020-01-02", "2020-01-03", retry_count=3)
            with self.assertRaises(ValueError):
                run_pilot(manifest, Path("data/processed/insider_research.db"), provider,
                          "2020-01-02", "2020-01-03")
            self.assertEqual(run_pilot(manifest, db, provider, "2020-01-02", "2020-01-03")["skipped"], 1)
            self.assertEqual(provider.calls, 2)
            conn = sqlite3.connect(db)
            self.assertEqual(conn.execute("SELECT security_id,ticker FROM pilot_prices").fetchone(), ("m1","AAA"))
            conn.close()
            checks = validate(db, root / "reports")
            self.assertFalse(any(c["severity"] == "FAIL" for c in checks))

            other_db = root / "recycled.db"
            recycled = RecycledProvider()
            result = run_pilot(manifest, other_db, recycled, "2020-01-02", "2020-01-03")
            self.assertEqual(result["identity_review"], 1)
            self.assertEqual(recycled.calls, 1)  # No price request after mismatch.
            conn = sqlite3.connect(other_db)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM pilot_prices").fetchone()[0], 0)
            conn.close()
            checks = validate(other_db, root / "recycled_reports")
            self.assertTrue(any(c["check"] == "unknown_or_mismatched_identity" and
                                c["severity"] == "FAIL" for c in checks))

            unknown_db = root / "unknown.db"
            unknown = UnknownProvider()
            result = run_pilot(manifest, unknown_db, unknown, "2020-01-02", "2020-01-03")
            self.assertEqual((result["unknown_symbol"], result["api_requests"]), (1, 1))
            conn = sqlite3.connect(unknown_db)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM pilot_prices").fetchone()[0], 0)
            conn.close()


if __name__ == "__main__":
    unittest.main()
