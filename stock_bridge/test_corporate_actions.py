"""Offline regression tests for corporate-action source failures and parsing."""
import copy
import io
import json
import tempfile
import unittest
import urllib.error
import urllib.parse
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import phase2 as p
import phase2_fixed as f


FIELDS = ["資料日期", "股票代號", "股票名稱", "除權息前收盤價", "除權息參考價"]
PAYLOAD = {"stat": "OK", "fields": FIELDS, "data": [
    ["115年07月24日", "2002", "中鋼", "19.10", "18.95"],
    ["115年07月24日", "3019", "亞光", "157", "152.4"],
]}


class Response:
    def __init__(self, body, status=200, content_type="application/json"):
        self.body, self.status = body, status
        self.headers = {"Content-Type": content_type}
    def read(self):
        return self.body
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False


class CorporateActionTests(unittest.TestCase):
    def setUp(self):
        self.parser = patch.object(p, "roc_date_to_iso", f.robust_roc_date_to_iso)
        self.parser.start()
        self.addCleanup(self.parser.stop)

    def test_security_html_and_http307_are_explicit_access_denials(self):
        body = b"<html>FOR SECURITY REASONS, THIS PAGE CAN NOT BE ACCESSED.</html>"
        outcomes = [Response(body, content_type="text/html"),
                    urllib.error.HTTPError("https://www.twse.com.tw/test", 307, "Temporary Redirect",
                                           {"Content-Type": "text/html"}, io.BytesIO(body))]
        for outcome in outcomes:
            with self.subTest(outcome=type(outcome).__name__):
                kwargs = {"side_effect": outcome} if isinstance(outcome, Exception) else {"return_value": outcome}
                with patch.object(p.urllib.request, "urlopen", **kwargs):
                    with self.assertRaises(p.SourceFetchError) as caught:
                        p.http_json("https://www.twse.com.tw/test")
                    self.assertEqual(caught.exception.category, "SOURCE_ACCESS_DENIED")
                    self.assertFalse(caught.exception.retryable)
                    self.assertTrue(caught.exception.details["security_denied"])

    def test_invalid_json_is_not_an_empty_success(self):
        for body in (b"", b"<html>unavailable</html>", b'{"value": NaN}'):
            with self.subTest(body=body), patch.object(p.urllib.request, "urlopen", return_value=Response(body)):
                with self.assertRaises(p.SourceFetchError) as caught:
                    p.http_json("https://www.twse.com.tw/test")
                self.assertEqual(caught.exception.category, "SOURCE_INVALID_JSON")

    def test_valid_json_with_bom_remains_supported(self):
        with patch.object(p.urllib.request, "urlopen", return_value=Response(b'\xef\xbb\xbf{"stat":"OK"}')):
            self.assertEqual(p.http_json("https://www.twse.com.tw/test"), {"stat": "OK"})

    def test_denial_stops_all_further_requests_and_preserves_diagnostics(self):
        fetcher = f.MarketFetcher()
        error = p.SourceFetchError("SOURCE_ACCESS_DENIED", "https://www.twse.com.tw/test", http_status=307)
        with patch.object(p, "http_json", side_effect=error) as get:
            for symbol in p.SYMBOLS:
                actions, errors, diag = f.corrected_fetch_corporate_actions(symbol, "2023-06-01", "2026-10-13", fetcher)
                self.assertFalse(actions)
                self.assertEqual(len(errors), 8)
                self.assertEqual(diag["fetch_error_count"], 8)
                self.assertTrue(diag["circuit_open"])
                self.assertIn("SOURCE_ACCESS_DENIED", diag["fetch_errors_sample"][0])
            self.assertEqual(get.call_count, 1)

    def test_rate_limit_is_not_retried(self):
        error = p.SourceFetchError("SOURCE_RATE_LIMITED", "https://www.twse.com.tw/test", http_status=429)
        with patch.object(p, "http_json", side_effect=error) as get, patch.object(f.time, "sleep") as sleep:
            with self.assertRaises(p.SourceFetchError):
                f.MarketFetcher().get("https://www.twse.com.tw/test")
            self.assertEqual(get.call_count, 1)
            sleep.assert_not_called()

    def test_transient_failure_retries_once_then_reuses_success(self):
        error = p.SourceFetchError("SOURCE_NETWORK_ERROR", "https://www.twse.com.tw/test", retryable=True)
        with patch.object(p, "http_json", side_effect=[error, PAYLOAD]) as get, patch.object(f.time, "sleep"):
            fetcher = f.MarketFetcher()
            self.assertEqual(fetcher.get("https://www.twse.com.tw/test"), PAYLOAD)
            self.assertEqual(fetcher.get("https://www.twse.com.tw/test"), PAYLOAD)
            self.assertEqual(get.call_count, 2)
            self.assertEqual(fetcher.cache_hits, 1)

    def test_persistent_transient_failure_is_bounded_and_cached(self):
        error = p.SourceFetchError("SOURCE_NETWORK_ERROR", "https://www.twse.com.tw/test", retryable=True)
        with patch.object(p, "http_json", side_effect=error) as get, patch.object(f.time, "sleep"):
            fetcher = f.MarketFetcher()
            for _ in range(2):
                with self.assertRaises(p.SourceFetchError):
                    fetcher.get("https://www.twse.com.tw/test")
            self.assertEqual(get.call_count, 2)

    def test_fetch_budget_prevents_new_requests(self):
        with patch.object(f.time, "monotonic", side_effect=[0, 91]), patch.object(p, "http_json") as get:
            with self.assertRaisesRegex(p.SourceFetchError, "SOURCE_FETCH_BUDGET_EXHAUSTED"):
                f.MarketFetcher().get("https://www.twse.com.tw/test")
            get.assert_not_called()

    def test_market_payload_is_shared_across_symbols(self):
        with patch.object(p, "http_json", return_value=PAYLOAD) as get:
            fetcher = f.MarketFetcher()
            for symbol in p.SYMBOLS:
                actions, errors, diag = f.corrected_fetch_corporate_actions(symbol, "2026-07-24", "2026-07-24", fetcher)
                self.assertFalse(errors)
                self.assertEqual(len(actions), 2)
                self.assertTrue(all(a["name"] == p.SYMBOLS[symbol] for a in actions))
            self.assertEqual(get.call_count, 2)
            self.assertEqual(fetcher.cache_hits, 2)

    def test_error_status_or_missing_schema_cannot_pass(self):
        for payload in ({}, {"stat": "ERROR"}, {"stat": "ERROR", "fields": FIELDS, "data": []},
                        {"stat": "OK", "fields": [], "data": []}, {"stat": "OK", "fields": FIELDS}):
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    f.validated_action_table(payload, "2002", "EX_RIGHT_DIVIDEND", "20260724", "20260724")
        empty = {"stat": "OK", "fields": FIELDS, "data": []}
        self.assertEqual(f.validated_action_table(empty, "2002", "EX_RIGHT_DIVIDEND", "20260724", "20260724"), [])

    def test_invalid_target_rows_and_out_of_range_dates_cannot_pass(self):
        for index, value in ((0, "115年02月31日"), (2, "錯誤名稱"), (3, "NaN"), (4, "0")):
            payload = copy.deepcopy(PAYLOAD)
            payload["data"][0][index] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                f.validated_action_table(payload, "2002", "EX_RIGHT_DIVIDEND", "20260724", "20260724")
        with self.assertRaisesRegex(ValueError, "OUTSIDE_REQUESTED_RANGE"):
            f.validated_action_table(PAYLOAD, "2002", "EX_RIGHT_DIVIDEND", "20261013", "20261013")

    def test_conflicting_events_fail_the_window(self):
        payload = copy.deepcopy(PAYLOAD)
        payload["data"].append(["115年07月24日", "2002", "中鋼", "19.10", "18.90"])
        with patch.object(p, "http_json", return_value=payload):
            _, errors, _ = f.corrected_fetch_corporate_actions("2002", "2026-07-24", "2026-07-24")
        self.assertTrue(any("SOURCE_CONFLICTING_ACTION" in error for error in errors))

    def test_fetch_failure_is_not_mislabelled_as_missing_event(self):
        data = {"analytics": {"2002": {"corporate_actions": {"events": [], "fetch_error_count": 1,
                "fetch_errors_sample": ["SOURCE_ACCESS_DENIED"]}}}, "bridge": {"analysis_ready": False}}
        with tempfile.TemporaryDirectory() as directory, patch.object(p, "LATEST_PATH", Path(directory)/"latest.json"), \
                patch.object(p, "run_phase2", return_value=data):
            with self.assertRaisesRegex(RuntimeError, "CORPORATE_ACTION_FETCH_FAILED.*SOURCE_ACCESS_DENIED"):
                f.run()
            self.assertFalse(json.loads(p.LATEST_PATH.read_text())["bridge"]["analysis_ready"])

    def test_missing_event_sentinel_remains_active_after_successful_fetch(self):
        data = {"analytics": {"2002": {"corporate_actions": {"events": [], "fetch_error_count": 0}}},
                "history": {"2002": {}}, "bridge": {"analysis_ready": True, "symbol_analysis_ready": {"2002": True}}}
        with tempfile.TemporaryDirectory() as directory, patch.object(p, "LATEST_PATH", Path(directory)/"latest.json"), \
                patch.object(p, "run_phase2", return_value=data), \
                patch.object(p, "load_history", return_value=[{"date": "2026-07-01"}, {"date": "2026-07-31"}]):
            with self.assertRaisesRegex(RuntimeError, "CORPORATE_ACTION_REGRESSION"):
                f.run()
            self.assertFalse(json.loads(p.LATEST_PATH.read_text())["bridge"]["analysis_ready"])

    def test_legacy_fallback_preserves_requested_window_and_shared_results(self):
        def market_response(url, **kwargs):
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            if "/rwd/" in url:
                raise p.SourceFetchError("SOURCE_NETWORK_ERROR", url, retryable=True)
            if query.get("startDate") != ["20260724"]:
                return {"stat": "查詢結束日期小於查詢開始日期，請重新查詢!"}
            if query.get("endDate") != ["20260724"]:
                return {"stat": "ERROR"}
            return copy.deepcopy(PAYLOAD)

        fetcher = f.MarketFetcher()
        with patch.object(p, "http_json", side_effect=market_response) as get, patch.object(f.time, "sleep"):
            for symbol in p.SYMBOLS:
                actions, errors, diag = f.corrected_fetch_corporate_actions(
                    symbol, "2026-07-24", "2026-07-24", fetcher)
                self.assertFalse(errors)
                self.assertEqual(len(actions), 2)
                self.assertTrue(all(a["date"] == "2026-07-24" for a in actions))
                self.assertEqual(diag["source_success_windows"],
                                 {"EX_RIGHT_DIVIDEND": 1, "CAPITAL_REDUCTION": 1})
            # Two bounded primary attempts, one valid fallback, one capital table.
            # The second symbol reuses the same payloads without new HTTP calls.
            self.assertEqual(get.call_count, 4)

    def test_primary_and_fallback_errors_remain_visible(self):
        def market_response(url, **kwargs):
            if "/rwd/" in url:
                raise p.SourceFetchError("SOURCE_NETWORK_ERROR", url, reason="read timed out")
            if "TWT49U" in url:
                return {"stat": "查詢結束日期小於查詢開始日期，請重新查詢!"}
            return {"stat": "OK", "fields": FIELDS, "data": []}

        with patch.object(p, "http_json", side_effect=market_response):
            actions, errors, diag = f.corrected_fetch_corporate_actions(
                "2002", "2026-07-24", "2026-07-24")
        self.assertEqual(actions, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("SOURCE_NETWORK_ERROR", errors[0])
        self.assertIn("SOURCE_INVALID_STATUS", errors[0])
        self.assertEqual(len(diag["endpoint_failures_sample"]), 2)
        self.assertTrue(any("/rwd/" in error for error in diag["endpoint_failures_sample"]))

    def test_complete_phase2_retains_pass_with_shared_valid_official_tables(self):
        def market_response(url, **kwargs):
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            start = (query.get("startDate") or query["strDate"])[0]
            end = query["endDate"][0]
            payload = copy.deepcopy(PAYLOAD)
            if "TWTAUU" in url or not start <= "20260724" <= end:
                payload["data"] = []
            return payload
        last = date(2026, 10, 13)
        rows = [{"date": (last-timedelta(days=816-i)).isoformat(), "open": 100,
                 "high": 101, "low": 99, "close": 100, "volume": 10} for i in range(817)]
        latest = {"bridge": {}, "history": {s: {"history_gate": "PASS", "long_term_750_ready": True}
                  for s in p.SYMBOLS}, "quotes": {s: {"status": "FAIL_NO_RECENT_TRADE"} for s in p.SYMBOLS}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/"latest.json").write_text(json.dumps(latest))
            for symbol in p.SYMBOLS:
                (root/f"{symbol}.json").write_text(json.dumps({"rows": rows}))
            with patch.object(p, "LATEST_PATH", root/"latest.json"), patch.object(p, "HISTORY_DIR", root), \
                    patch.object(p, "http_json", side_effect=market_response) as get:
                output = f.run()
            self.assertTrue(output["bridge"]["analysis_ready"])
            for symbol in p.SYMBOLS:
                actions = output["analytics"][symbol]["corporate_actions"]
                self.assertEqual(actions["status"], "PASS")
                self.assertEqual(actions["event_count"], 1)
                self.assertEqual(actions["fetch_error_count"], 0)
                self.assertEqual(actions["events"][0]["name"], p.SYMBOLS[symbol])
            self.assertEqual(get.call_count, 6)


if __name__ == "__main__":
    unittest.main()

