from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
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
    @staticmethod
    def reference(x: float, iv: float = 0.5) -> core.ReferencePoint:
        return core.ReferencePoint(
            strike=100.0,
            x=x,
            iv=iv,
            iv_width=0.01,
            depth=10.0,
            option_type="P" if x < 0 else "C",
        )

    def test_one_tick_local_signal(self) -> None:
        rows = core.one_tick_rows(
            [node("TEST-C", 100, "C", 1.0, 1.1)],
            "TestVenue",
            {"TEST-C": "BUY"},
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["side"], "BUY")
        self.assertEqual(rows[0]["confirmation"], "LOCAL")

    def test_local_net_survives_when_pcp_is_not_profitable(self) -> None:
        args = SimpleNamespace(
            min_edge_ticks=5.0,
            min_parity_edge_ticks=1.0,
            min_size=1.0,
            min_vega_ticks_per_pp=1.0,
        )
        row = {
            "basis": "LOCAL",
            "net_edge_ticks": 8.0,
            "size": 5.0,
            "parity_fees_complete": True,
            "parity_net_ticks": -1.0,
            "parity_size": 5.0,
            "vega_ticks_per_pp": 3.0,
        }
        self.assertTrue(core.is_p1_local(row, args))
        self.assertFalse(core.is_pcp_net(row, args))

    def test_pcp_roundtrip_deducts_options_and_hedge_taker_fees(self) -> None:
        call = replace(
            node("TEST-C", 90, "C", 11.7, 11.8),
            index_price=100.0,
            taker_fee_rate=0.001,
            trade_fee_cap_rate=1.0,
        )
        put = replace(
            node("TEST-P", 90, "P", 2.0, 2.1),
            index_price=100.0,
            taker_fee_rate=0.001,
            trade_fee_cap_rate=1.0,
        )
        lookup = {
            (item.underlying, item.expiry, item.strike, item.option_type): item
            for item in (call, put)
        }
        metrics = core.executable_parity_metrics(
            put,
            "SELL",
            lookup,
            hedge_taker_rate=0.0005,
            hedge_leverage=10.0,
        )
        self.assertIsNotNone(metrics)
        self.assertAlmostEqual(metrics["raw_ticks"], 2.0)  # type: ignore[index]
        self.assertAlmostEqual(metrics["net_ticks"], -3.0)  # type: ignore[index]
        self.assertTrue(metrics["fees_complete"])  # type: ignore[index]

    def test_local_fit_rejects_distant_sparse_bracket(self) -> None:
        points = [
            self.reference(-0.45, 0.60),
            self.reference(-0.35, 0.56),
            self.reference(0.12, 0.44),
            self.reference(0.18, 0.43),
        ]
        fit = core.weighted_local_fit(
            0.0,
            points,
            neighbors_each_side=3,
            min_neighbors=4,
            allow_one_sided=False,
            max_neighbor_distance=0.20,
            max_bracket_span=0.30,
        )
        self.assertIsNone(fit)

    def test_local_fit_accepts_dense_bracket(self) -> None:
        points = [
            self.reference(-0.12, 0.54),
            self.reference(-0.06, 0.52),
            self.reference(0.05, 0.49),
            self.reference(0.11, 0.48),
        ]
        fit = core.weighted_local_fit(
            0.0,
            points,
            neighbors_each_side=3,
            min_neighbors=4,
            allow_one_sided=False,
            max_neighbor_distance=0.20,
            max_bracket_span=0.30,
        )
        self.assertIsNotNone(fit)

    def test_local_quality_flags_pair_conflict_and_thin_exit(self) -> None:
        candidate = replace(node("TEST-P", 90, "P", 2.0, 2.1), bid_size=0.01)
        flags = core.local_quality_flags(
            candidate,
            "BUY",
            quote_iv=0.52,
            paired_iv=0.50,
            pair_iv_tolerance=0.005,
            min_exit_depth=0.1,
        )
        self.assertEqual(flags, ["PAIR_CONFLICT", "EXIT_THIN"])

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
