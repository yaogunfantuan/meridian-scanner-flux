from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import option_alert_daemon as daemon
import option_scanner_core as core


def node(
    instrument: str,
    strike: float,
    option_type: str,
    bid: float,
    ask: float,
    forward: float | None = 100.0,
) -> core.Node:
    return core.Node(
        instrument=instrument,
        underlying="TEST",
        expiry="20300101",
        expiry_ts=1893456000,
        strike=strike,
        option_type=option_type,
        tick=0.1,
        bid=bid,
        bid_size=10.0,
        ask=ask,
        ask_size=12.0,
        mark=(bid + ask) / 2,
        bid_iv=0.49,
        ask_iv=0.51,
        mark_iv=0.50,
        forward=forward,
        discount=1.0,
    )


class ScannerCoreTests(unittest.TestCase):
    def test_one_tick_local_signal(self) -> None:
        rows = core.one_tick_rows(
            [node("TEST-C", 100, "C", 1.0, 1.1)],
            "TestVenue",
            {"TEST-C": "BUY"},
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["side"], "BUY")
        self.assertEqual(rows[0]["confirmation"], "LOCAL")

    def test_p1_requires_parity_size_and_vega(self) -> None:
        args = SimpleNamespace(
            min_parity_edge_ticks=1.0,
            min_size=1.0,
            min_vega_ticks_per_pp=1.0,
        )
        row = {
            "basis": "LOCAL",
            "parity_edge_ticks": 2.0,
            "parity_size": 5.0,
            "vega_ticks_per_pp": 3.0,
        }
        self.assertTrue(core.is_p1_local(row, args))
        row["parity_edge_ticks"] = -1.0
        self.assertFalse(core.is_p1_local(row, args))

    def test_put_call_calibration_recovers_forward_and_discount(self) -> None:
        nodes = []
        for strike in (80.0, 90.0, 100.0, 110.0):
            put_mid = max(strike - 100.0, 0.0) + 5.0
            call_mid = put_mid + 100.0 - strike
            nodes.extend(
                [
                    node(f"C-{strike:g}", strike, "C", call_mid - 0.1, call_mid + 0.1, None),
                    node(f"P-{strike:g}", strike, "P", put_mid - 0.1, put_mid + 0.1, None),
                ]
            )
        calibrated, values = core.calibrate_expiry_carry(nodes)
        calibration = values[("TEST", "20300101")]
        self.assertAlmostEqual(calibration["forward"], 100.0)
        self.assertAlmostEqual(calibration["discount"], 1.0)
        self.assertTrue(all(item.forward == 100.0 for item in calibrated))

    def test_feishu_signature_is_stable(self) -> None:
        self.assertEqual(
            daemon.feishu_sign("1599360473", "test-secret"),
            "wSds2BzzFIIGf/WrhUO+NI1q/9j+FRJd3JNHKAq0NZY=",
        )

    def test_jsonl_log_rotates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "alerts.jsonl"
            for index in range(4):
                daemon.append_log(path, {"index": index, "text": "x" * 700}, 0.001)
            self.assertTrue(path.exists())
            self.assertTrue(path.with_suffix(".jsonl.1").exists())


if __name__ == "__main__":
    unittest.main()
