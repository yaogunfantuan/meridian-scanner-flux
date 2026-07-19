#!/usr/bin/env python3
"""Periodic REST option-surface scans with optional Feishu alerts."""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence, Tuple
from urllib.request import Request, urlopen

import exchange_adapters as adapters
import option_scanner_core as core
import scanner_http


CACHE_VERSION = 1


class CatalogManager:
    """Keep each venue's instrument metadata in a restart-safe TTL cache."""

    def __init__(
        self,
        path: Path,
        ttl: float,
        workers: int,
        underlying: str | None,
        pacer: adapters.RequestPacer,
    ) -> None:
        self.path = path
        self.ttl = ttl
        self.workers = workers
        self.underlying = underlying
        self.pacer = pacer
        self.cache = self._read()

    def _read(self) -> Dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if value.get("version") == CACHE_VERSION and isinstance(value.get("venues"), dict):
                return value
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError) as exc:
            print(f"合约缓存不可用，将重新获取（{exc}）", file=sys.stderr)
        return {"version": CACHE_VERSION, "venues": {}}

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(self.cache, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(temporary, self.path)

    def ensure(self, venues: Sequence[str]) -> Tuple[Dict[str, Any], List[str]]:
        now = time.time()
        entries = self.cache.setdefault("venues", {})
        refreshed: List[str] = []
        changed = False
        for venue in venues:
            entry = entries.get(venue)
            same_scope = bool(entry and entry.get("underlying") == self.underlying)
            age = now - float(entry.get("fetched_at", 0)) if same_scope else float("inf")
            if same_scope and entry.get("data") and age < self.ttl:
                continue
            try:
                data = adapters.fetch_catalog(
                    venue, self.pacer, self.workers, self.underlying
                )
                if not data:
                    raise RuntimeError("没有返回活跃期权")
                entries[venue] = {
                    "fetched_at": now,
                    "underlying": self.underlying,
                    "data": data,
                }
                refreshed.append(venue)
                changed = True
            except Exception as exc:
                if same_scope and entry and entry.get("data"):
                    print(
                        f"{adapters.LABELS[venue]}: 目录刷新失败，继续使用旧缓存（{exc}）",
                        file=sys.stderr,
                    )
                else:
                    print(
                        f"{adapters.LABELS[venue]}: 无可用合约目录（{exc}）",
                        file=sys.stderr,
                    )
        if changed:
            self._write()
        available = {
            venue: entries[venue]["data"]
            for venue in venues
            if venue in entries
            and entries[venue].get("underlying") == self.underlying
            and entries[venue].get("data")
        }
        return available, refreshed


def actionable_one_ticks(
    rows: Iterable[Dict[str, Any]],
    p1_keys: set[Tuple[str, str, str]],
    mark_gap_ticks: float,
) -> List[Dict[str, Any]]:
    actionable: List[Dict[str, Any]] = []
    for original in rows:
        row = dict(original)
        key = (row["venue"], row["instrument"], row["side"])
        if key in p1_keys:
            continue
        if row["confirmation"] == "LOCAL" and row["side"] != "NEUTRAL":
            row["alert_basis"] = "LOCAL"
            actionable.append(row)
            continue
        mark = row.get("mark")
        tick = row.get("tick")
        if not mark or not tick:
            continue
        buy_gap = (mark - row["ask"]) / tick
        sell_gap = (row["bid"] - mark) / tick
        if buy_gap >= mark_gap_ticks and buy_gap >= sell_gap:
            row["side"] = "BUY"
            row["mark_gap"] = buy_gap
        elif sell_gap >= mark_gap_ticks:
            row["side"] = "SELL"
            row["mark_gap"] = sell_gap
        else:
            continue
        row["alert_basis"] = "MARK"
        actionable.append(row)
    return sorted(
        actionable,
        key=lambda row: (row["alert_basis"] == "LOCAL", row["mark_gap"], row["depth"]),
        reverse=True,
    )


def scan_nodes(
    venue: str,
    nodes: Sequence[core.Node],
    active: int,
    seconds: float,
    states: Dict[Tuple[str, str], core.QuoteState],
    args: argparse.Namespace,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]]]:
    now = time.time()
    core.update_quote_states(nodes, states, now)
    candidates = core.find_candidates(nodes, states, args, now)
    label = adapters.LABELS[venue]
    for row in candidates:
        row["venue"] = label
    p1_rows = [row for row in candidates if core.is_p1_local(row, args)]
    for row in p1_rows:
        row["mode"] = "PCP_NET" if core.is_pcp_net(row, args) else "LOCAL_NET"
    p1_rows.sort(
        key=lambda row: (
            row["mode"] == "PCP_NET",
            not bool(row.get("quality_flags")),
            (
                row.get("parity_net_usdt")
                if row["mode"] == "PCP_NET"
                else row.get("single_net_usdt")
            ) or 0.0,
            row["score"],
        ),
        reverse=True,
    )
    local_signals = {
        row["instrument"]: row["signal"]
        for row in candidates
        if row["basis"] == "LOCAL"
    }
    one_ticks = core.one_tick_rows(nodes, label, local_signals)
    one_tick_keys = {
        (row["venue"], row["instrument"], row["side"])
        for row in one_ticks
        if row["side"] != "NEUTRAL"
    }
    for row in p1_rows:
        row["one_tick"] = (row["venue"], row["instrument"], row["signal"]) in one_tick_keys
    p1_keys = {(row["venue"], row["instrument"], row["signal"]) for row in p1_rows}
    tick_alerts = actionable_one_ticks(one_ticks, p1_keys, args.mark_fallback_ticks)
    summary = {
        "venue": label,
        "active": active,
        "returned": len(nodes),
        "two_sided": sum(node.valid_bid and node.valid_ask for node in nodes),
        "p1": len(p1_rows),
        "pcp_net": sum(row["mode"] == "PCP_NET" for row in p1_rows),
        "local_net": sum(row["mode"] == "LOCAL_NET" for row in p1_rows),
        "one_tick_alerts": len(tick_alerts),
        "one_tick_all": len(one_ticks),
        "seconds": seconds,
    }
    return summary, p1_rows, tick_alerts


def print_tick_alerts(rows: Sequence[Dict[str, Any]], limit: int) -> None:
    shown = list(rows[:limit])
    print(f"\n有效 1-tick 异常: {len(rows)}（显示前 {len(shown)}）")
    core.print_table(
        ("venue", "instrument", "side", "basis", "bid", "ask", "size", "mark", "mark_gap_ticks"),
        [
            (
                row["venue"], row["instrument"], row["side"], row["alert_basis"],
                row["bid"], row["ask"],
                row["ask_size"] if row["side"] == "BUY" else row["bid_size"],
                row["mark"], row["mark_gap"],
            )
            for row in shown
        ],
    )


def format_feishu_message(
    stamp: str,
    p1_rows: Sequence[Dict[str, Any]],
    tick_rows: Sequence[Dict[str, Any]],
    summaries: Sequence[Dict[str, Any]],
    elapsed: float,
    ticker_bytes: int,
    limit: int,
) -> str:
    lines = [
        f"【期权曲面扫描】{stamp}",
        f"耗时 {elapsed:.1f}s；ticker {ticker_bytes / 1024 / 1024:.2f} MiB",
    ]
    if p1_rows:
        lines.append(f"\n手续费后可执行候选：{len(p1_rows)}")
        for row in p1_rows[:limit]:
            one_tick = " [1-tick]" if row.get("one_tick") else ""
            if row["mode"] == "PCP_NET":
                detail = (
                    f"PCP净 {row['parity_net_ticks']:.1f}t / "
                    f"{row['parity_net_usdt']:.2f} USDT"
                )
            else:
                detail = (
                    f"单腿毛 {row['single_gross_usdt']:.2f}；"
                    f"费×2 {row['single_fee_usdt']:.2f}；"
                    f"净 {row['single_net_usdt']:.2f} USDT"
                )
            quality = (
                f"；风险 {','.join(row['quality_flags'])}"
                if row.get("quality_flags")
                else ""
            )
            lines.append(
                f"{row['venue']} {row['instrument']} {row['signal']} @ {row['price']:g} "
                f"× {row['size']:g}；{row['mode']}；{detail}{quality}{one_tick}"
            )
    if tick_rows:
        remaining = max(limit - min(len(p1_rows), limit), 0)
        lines.append(f"\n有效 1-tick 异常：{len(tick_rows)}")
        for row in tick_rows[:remaining]:
            price = row["ask"] if row["side"] == "BUY" else row["bid"]
            size = row["ask_size"] if row["side"] == "BUY" else row["bid_size"]
            lines.append(
                f"{row['venue']} {row['instrument']} {row['side']} @ {price:g} × {size:g}；"
                f"{row['alert_basis']}；mark差 {row['mark_gap']:.1f}t"
            )
    lines.append(
        "\n覆盖 "
        + "；".join(
            f"{row['venue']} {row['returned']}/{row['active']}，"
            f"PCP {row['pcp_net']} / LOCAL {row['local_net']}"
            for row in summaries
        )
    )
    return "\n".join(lines)


def feishu_sign(timestamp: str, secret: str) -> str:
    key = f"{timestamp}\n{secret}".encode("utf-8")
    digest = hmac.new(key, b"", digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def send_feishu(webhook: str, secret: str | None, message: str) -> None:
    payload: Dict[str, Any] = {"msg_type": "text", "content": {"text": message}}
    if secret:
        timestamp = str(int(time.time()))
        payload["timestamp"] = timestamp
        payload["sign"] = feishu_sign(timestamp, secret)
    request = Request(
        webhook,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urlopen(request, timeout=15) as response:
        result = json.loads(response.read().decode("utf-8"))
    code = result.get("code", result.get("StatusCode", 0))
    if code not in (0, "0"):
        raise RuntimeError(f"飞书返回错误：{result}")


def append_log(path: Path, record: Dict[str, Any], max_megabytes: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
    max_bytes = int(max_megabytes * 1024 * 1024)
    if max_bytes > 0 and path.exists() and path.stat().st_size + len(encoded.encode("utf-8")) > max_bytes:
        rotated = path.with_suffix(path.suffix + ".1")
        rotated.unlink(missing_ok=True)
        os.replace(path, rotated)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(encoded)


def add_scan_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--neighbors", type=int, default=3)
    parser.add_argument("--min-neighbors", type=int, default=4)
    parser.add_argument("--allow-one-sided", action="store_true")
    parser.add_argument(
        "--max-neighbor-log-distance",
        type=float,
        default=0.20,
        help="LOCAL参考点距目标log(K/F)的最大距离，默认0.20",
    )
    parser.add_argument(
        "--max-bracket-log-span",
        type=float,
        default=0.30,
        help="LOCAL最近左右参考点的最大log(K/F)跨度，默认0.30",
    )
    parser.add_argument("--max-reference-iv-width", type=float, default=0.20)
    parser.add_argument("--min-reference-depth", type=float, default=0.1)
    parser.add_argument(
        "--pair-iv-tolerance",
        type=float,
        default=0.005,
        help="同执行价另一种期权IV冲突容忍度，默认0.5个百分点",
    )
    parser.add_argument(
        "--min-exit-depth",
        type=float,
        default=0.1,
        help="低于该当前退出侧深度时标记EXIT_THIN，默认0.1",
    )
    parser.add_argument("--min-size", type=float, default=1.0)
    parser.add_argument("--min-iv-edge", type=float, default=0.01)
    parser.add_argument("--min-z", type=float, default=3.0)
    parser.add_argument("--z-floor", type=float, default=0.005)
    parser.add_argument("--cost-ticks", type=float, default=2.0)
    parser.add_argument("--min-edge-ticks", type=float, default=5.0)
    parser.add_argument("--mark-fallback-ticks", type=float, default=5.0)
    parser.add_argument("--mark-outlier-z", type=float, default=2.0)
    parser.add_argument("--mark-regime-min-points", type=int, default=4)
    parser.add_argument("--mark-regime-min-bias", type=float, default=0.02)
    parser.add_argument("--min-parity-edge-ticks", type=float, default=1.0)
    parser.add_argument("--min-vega-ticks-per-pp", type=float, default=1.0)
    parser.add_argument(
        "--hedge-taker-rate",
        type=float,
        default=0.0005,
        help="PCP回转估算中的Delta对冲单边Taker费率，默认0.05%%",
    )
    parser.add_argument(
        "--hedge-leverage",
        type=float,
        default=10.0,
        help="经典保证金资金占用估算使用的对冲杠杆，默认10倍",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="每分钟用 REST 快照扫描期权曲面并可选推送飞书")
    parser.add_argument("--exchange", choices=("all",) + adapters.VENUES, default="all")
    parser.add_argument("--underlying", type=str.upper)
    parser.add_argument("--interval", type=float, default=60.0)
    parser.add_argument("--catalog-ttl", type=float, default=21600.0)
    parser.add_argument("--cache", type=Path, default=Path("work/option_catalog.json"))
    parser.add_argument("--log", type=Path, default=Path("outputs/option_alerts.jsonl"))
    parser.add_argument("--max-log-mb", type=float, default=100.0)
    parser.add_argument("--no-log", action="store_true")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--rate-limit", type=float, default=8.0)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--alert-limit", type=int, default=20)
    parser.add_argument("--once", action="store_true")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--notify", action="store_true", help="向飞书发送；默认只在终端输出")
    mode.add_argument("--dry-run", action="store_true", help="明确只在终端输出（默认行为）")
    add_scan_arguments(parser)
    args = parser.parse_args()
    if args.interval <= 0 or args.catalog_ttl <= 0 or args.max_log_mb < 0:
        parser.error("interval、catalog-ttl 必须大于 0，max-log-mb 不能小于 0")
    if args.workers < 1 or not 0 < args.rate_limit <= 10:
        parser.error("workers 至少为 1，rate-limit 必须在 0 到 10 之间")
    if args.hedge_taker_rate < 0 or args.hedge_leverage <= 0:
        parser.error("hedge-taker-rate 不能小于 0，hedge-leverage 必须大于 0")
    if args.max_neighbor_log_distance <= 0 or args.max_bracket_log_span <= 0:
        parser.error("LOCAL邻居距离和左右跨度必须大于 0")
    if args.pair_iv_tolerance < 0 or args.min_exit_depth < 0:
        parser.error("pair-iv-tolerance 和 min-exit-depth 不能小于 0")
    if args.notify and not os.environ.get("FEISHU_WEBHOOK_URL"):
        parser.error("--notify 需要环境变量 FEISHU_WEBHOOK_URL")
    return args


def main() -> int:
    args = parse_args()
    venues = adapters.VENUES if args.exchange == "all" else (args.exchange,)
    pacer = adapters.RequestPacer(args.rate_limit)
    catalog_manager = CatalogManager(
        args.cache, args.catalog_ttl, args.workers, args.underlying, pacer
    )
    states: Dict[str, Dict[Tuple[str, str], core.QuoteState]] = {
        venue: {} for venue in venues
    }
    webhook = os.environ.get("FEISHU_WEBHOOK_URL", "")
    secret = os.environ.get("FEISHU_WEBHOOK_SECRET")

    try:
        while True:
            round_started = time.time()
            before_catalog = scanner_http.total_response_bytes()
            catalogs, refreshed = catalog_manager.ensure(venues)
            after_catalog = scanner_http.total_response_bytes()
            if not catalogs:
                print("没有可用的交易所合约目录。", file=sys.stderr)
                return 1

            snapshots: Dict[str, Tuple[List[core.Node], int, float]] = {}
            with ThreadPoolExecutor(max_workers=len(catalogs)) as executor:
                futures = {
                    executor.submit(
                        adapters.load_snapshot, venue, catalog, pacer, args.workers
                    ): venue
                    for venue, catalog in catalogs.items()
                }
                for future in as_completed(futures):
                    venue = futures[future]
                    try:
                        snapshots[venue] = future.result()
                    except Exception as exc:
                        print(
                            f"{adapters.LABELS[venue]}: 本轮行情失败（{exc}）",
                            file=sys.stderr,
                        )
            after_tickers = scanner_http.total_response_bytes()

            summaries: List[Dict[str, Any]] = []
            p1_rows: List[Dict[str, Any]] = []
            tick_rows: List[Dict[str, Any]] = []
            for venue in venues:
                if venue not in snapshots:
                    continue
                nodes, active, seconds = snapshots[venue]
                summary, current_p1, current_ticks = scan_nodes(
                    venue, nodes, active, seconds, states[venue], args
                )
                summaries.append(summary)
                p1_rows.extend(current_p1)
                tick_rows.extend(current_ticks)

            p1_rows.sort(
                key=lambda row: (
                    row["mode"] == "PCP_NET",
                    not bool(row.get("quality_flags")),
                    (
                        row.get("parity_net_usdt")
                        if row["mode"] == "PCP_NET"
                        else row.get("single_net_usdt")
                    ) or 0.0,
                    row["score"],
                ),
                reverse=True,
            )
            tick_rows.sort(
                key=lambda row: (row["alert_basis"] == "LOCAL", row["mark_gap"], row["depth"]),
                reverse=True,
            )
            elapsed = time.time() - round_started
            catalog_bytes = after_catalog - before_catalog
            ticker_bytes = after_tickers - after_catalog
            stamp = datetime.now().astimezone().isoformat(timespec="seconds")
            estimated_gib_day = ticker_bytes * 1440 / 1024 / 1024 / 1024

            print(
                f"\n[{stamp}] 本轮 {elapsed:.1f}s；目录刷新: "
                f"{','.join(adapters.LABELS[item] for item in refreshed) or '-'}；"
                f"目录下载 {catalog_bytes / 1024 / 1024:.2f} MiB；"
                f"ticker 下载 {ticker_bytes / 1024 / 1024:.2f} MiB；"
                f"按当前轮估算 {estimated_gib_day:.2f} GiB/日"
            )
            core.print_table(
                (
                    "venue", "active", "returned", "two_sided", "candidates",
                    "PCP_NET", "LOCAL_NET", "1tick_alert", "1tick_all", "seconds",
                ),
                [
                    (
                        row["venue"], row["active"], row["returned"], row["two_sided"],
                        row["p1"], row["pcp_net"], row["local_net"],
                        row["one_tick_alerts"], row["one_tick_all"], row["seconds"],
                    )
                    for row in summaries
                ],
            )
            core.print_candidate_section("手续费后可执行候选", p1_rows, args.limit)
            print_tick_alerts(tick_rows, args.limit)

            record = {
                "timestamp": stamp,
                "elapsed_seconds": elapsed,
                "catalog_refreshed": refreshed,
                "catalog_response_bytes": catalog_bytes,
                "ticker_response_bytes": ticker_bytes,
                "ticker_estimated_gib_day": estimated_gib_day,
                "summaries": summaries,
                "p1": p1_rows,
                "one_tick_alerts": tick_rows,
            }
            if not args.no_log:
                append_log(args.log, record, args.max_log_mb)

            if p1_rows or tick_rows:
                message = format_feishu_message(
                    stamp, p1_rows, tick_rows, summaries, elapsed, ticker_bytes, args.alert_limit
                )
                if args.notify:
                    try:
                        send_feishu(webhook, secret, message)
                        print("飞书：已发送 1 条合并提醒。")
                    except Exception as exc:
                        print(f"飞书发送失败（{exc}）", file=sys.stderr)
                else:
                    print("飞书：dry-run，未发送。")

            if args.once:
                return 0 if summaries else 1
            wait_seconds = max(args.interval - (time.time() - round_started), 0.0)
            print(f"下一轮等待 {wait_seconds:.1f}s；不会与当前轮重叠。")
            time.sleep(wait_seconds)
    except KeyboardInterrupt:
        print("\n扫描已停止。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
