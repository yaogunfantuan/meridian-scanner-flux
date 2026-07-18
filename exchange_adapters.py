"""Public REST adapters that normalize Derive, Bybit, and Gate options."""

from __future__ import annotations

import json
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from http.client import HTTPException
from typing import Any, Dict, List, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import option_scanner_core as core
import scanner_http


VENUES = ("derive", "bybit", "gate")
LABELS = {"derive": "Derive", "bybit": "Bybit", "gate": "Gate.io"}
BYBIT_OPTION_COINS = ("BTC", "ETH", "SOL", "MNT", "XRP", "DOGE", "XAUT")
DERIVE_API = "https://api.lyra.finance"
BYBIT_API = "https://api.bybit.com"
GATE_API = "https://api.gateio.ws/api/v4"
USER_AGENT = "option-surface-alert/2.0"


class RequestPacer:
    """Share a conservative request-start rate across Derive worker threads."""

    def __init__(self, requests_per_second: float) -> None:
        self.interval = 1.0 / requests_per_second
        self.next_start = 0.0
        self.lock = threading.Lock()

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            start = max(now, self.next_start)
            self.next_start = start + self.interval
        delay = start - now
        if delay > 0:
            time.sleep(delay)


def fnum(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def derive_rpc(
    pacer: RequestPacer,
    method: str,
    params: Dict[str, Any],
    retries: int = 3,
) -> Any:
    pacer.wait()
    request = Request(
        f"{DERIVE_API}/{method}",
        data=json.dumps(params).encode("utf-8"),
        headers={
            "Accept-Encoding": "gzip",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    for attempt in range(retries + 1):
        try:
            with urlopen(request, timeout=20) as response:
                payload = scanner_http.read_json(response)
            if "error" in payload:
                raise RuntimeError(f"Derive API error: {payload['error']}")
            return payload["result"]
        except HTTPError as exc:
            if exc.code == 429 and attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise RuntimeError(f"Derive HTTP {exc.code}: {exc.reason}") from exc
        except (URLError, TimeoutError, HTTPException, OSError, json.JSONDecodeError) as exc:
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise RuntimeError(f"Derive network error: {exc}") from exc
    raise AssertionError("unreachable")


def fetch_derive_catalog(
    pacer: RequestPacer,
    workers: int,
    underlying: str | None,
) -> Dict[str, Dict[str, Dict[str, Any]]]:
    def load_page(page: int) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "instrument_type": "option",
            "expired": False,
            "page": page,
            "page_size": 1000,
        }
        if underlying:
            params["currency"] = underlying
        return derive_rpc(pacer, "public/get_all_instruments", params)

    first = load_page(1)
    results = [first]
    pages = int(first["pagination"]["num_pages"])
    if pages > 1:
        with ThreadPoolExecutor(max_workers=min(workers, pages - 1)) as executor:
            futures = [executor.submit(load_page, page) for page in range(2, pages + 1)]
            for future in as_completed(futures):
                results.append(future.result())

    option_sets: Dict[str, Dict[str, Dict[str, Any]]] = defaultdict(dict)
    for result in results:
        for instrument in result["instruments"]:
            if instrument.get("is_active"):
                currency = str(instrument["base_currency"])
                option_sets[currency][instrument["instrument_name"]] = instrument
    return dict(option_sets)


def derive_expiry(meta: Dict[str, Any]) -> str:
    timestamp = int(meta["option_details"]["expiry"])
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y%m%d")


def ticker_value(ticker: Dict[str, Any], long_key: str, slim_key: str) -> Any:
    value = ticker.get(long_key)
    return ticker.get(slim_key) if value is None else value


def derive_parse_tickers(
    underlying: str,
    options: Dict[str, Dict[str, Any]],
    pacer: RequestPacer,
    workers: int,
) -> List[core.Node]:
    groups: Dict[str, List[str]] = defaultdict(list)
    for name, instrument in options.items():
        groups[derive_expiry(instrument)].append(name)

    def fetch_group(expiry: str, names: List[str]) -> Tuple[str, List[str], Any]:
        result = derive_rpc(
            pacer,
            "public/get_tickers",
            {"instrument_type": "option", "currency": underlying, "expiry_date": expiry},
        )
        return expiry, names, result

    responses: List[Tuple[str, List[str], Any]] = []
    with ThreadPoolExecutor(max_workers=min(workers, max(len(groups), 1))) as executor:
        futures = {
            executor.submit(fetch_group, expiry, names): expiry
            for expiry, names in sorted(groups.items())
        }
        for future in as_completed(futures):
            expiry = futures[future]
            try:
                responses.append(future.result())
            except Exception as exc:
                print(f"{underlying}-{expiry}: 本轮到期日跳过（{exc}）", file=sys.stderr)

    nodes: List[core.Node] = []
    for expiry, names, result in sorted(responses):
        tickers = result.get("tickers", result)
        for name in names:
            ticker = tickers.get(name)
            if not ticker:
                continue
            meta = options[name]
            details = meta["option_details"]
            pricing = ticker.get("option_pricing") or {}
            nodes.append(
                core.Node(
                    instrument=name,
                    underlying=underlying,
                    expiry=expiry,
                    expiry_ts=int(details["expiry"]),
                    strike=float(details["strike"]),
                    option_type=str(details["option_type"]).upper(),
                    tick=float(meta["tick_size"]),
                    bid=fnum(ticker_value(ticker, "best_bid_price", "b")),
                    bid_size=fnum(ticker_value(ticker, "best_bid_amount", "B")),
                    ask=fnum(ticker_value(ticker, "best_ask_price", "a")),
                    ask_size=fnum(ticker_value(ticker, "best_ask_amount", "A")),
                    mark=fnum(ticker_value(ticker, "mark_price", "M")),
                    bid_iv=fnum(ticker_value(pricing, "bid_iv", "bi")),
                    ask_iv=fnum(ticker_value(pricing, "ask_iv", "ai")),
                    mark_iv=fnum(ticker_value(pricing, "iv", "i")),
                    forward=fnum(ticker_value(pricing, "forward_price", "f")),
                    discount=fnum(ticker_value(pricing, "discount_factor", "df")) or 1.0,
                )
            )
    return nodes


def bybit_get(path: str, params: Dict[str, Any]) -> Any:
    request = Request(
        f"{BYBIT_API}{path}?{urlencode(params)}",
        headers={"Accept-Encoding": "gzip", "User-Agent": USER_AGENT},
    )
    for attempt in range(4):
        try:
            with urlopen(request, timeout=20) as response:
                payload = scanner_http.read_json(response)
            if payload.get("retCode") != 0:
                raise RuntimeError(f"Bybit: {payload.get('retMsg')}")
            return payload["result"]
        except (OSError, HTTPException, json.JSONDecodeError):
            if attempt == 3:
                raise
            time.sleep(1.5 * (attempt + 1))
    raise AssertionError("unreachable")


def bybit_instruments(coin: str) -> Dict[str, Dict[str, Any]]:
    output: Dict[str, Dict[str, Any]] = {}
    cursor = ""
    while True:
        params = {"category": "option", "baseCoin": coin, "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        result = bybit_get("/v5/market/instruments-info", params)
        output.update(
            {item["symbol"]: item for item in result["list"] if item.get("status") == "Trading"}
        )
        cursor = result.get("nextPageCursor", "")
        if not cursor:
            return output


def fetch_bybit_catalog(
    workers: int,
    underlying: str | None,
) -> Dict[str, Dict[str, Dict[str, Any]]]:
    coins = [underlying] if underlying else list(BYBIT_OPTION_COINS)
    found: Dict[str, Dict[str, Dict[str, Any]]] = {}
    with ThreadPoolExecutor(max_workers=min(workers, max(len(coins), 1))) as executor:
        futures = {executor.submit(bybit_instruments, coin): coin for coin in coins}
        for future in as_completed(futures):
            coin = futures[future]
            try:
                instruments = future.result()
                if instruments:
                    found[coin] = instruments
            except Exception as exc:
                print(f"Bybit {coin}: 合约目录跳过（{exc}）", file=sys.stderr)
    return found


def bybit_strike(symbol: str) -> float:
    parts = symbol.split("-")
    option_index = next(
        (index for index in range(len(parts) - 1, -1, -1) if parts[index] in {"C", "P"}),
        None,
    )
    if option_index is None or option_index < 1:
        raise ValueError(f"无法解析 Bybit 期权名: {symbol}")
    return float(parts[option_index - 1].replace("_", "."))


def gate_get(path: str, params: Dict[str, Any]) -> Any:
    request = Request(
        f"{GATE_API}{path}?{urlencode(params)}",
        headers={"Accept-Encoding": "gzip", "User-Agent": USER_AGENT},
    )
    for attempt in range(4):
        try:
            with urlopen(request, timeout=20) as response:
                return scanner_http.read_json(response)
        except (OSError, HTTPException, json.JSONDecodeError):
            if attempt == 3:
                raise
            time.sleep(1.5 * (attempt + 1))
    raise AssertionError("unreachable")


def fetch_gate_catalog(
    workers: int,
    underlying: str | None,
) -> Dict[str, Dict[str, Dict[str, Any]]]:
    if underlying:
        normalized = underlying.upper()
        underlyings = [normalized if "_" in normalized else normalized + "_USDT"]
    else:
        underlyings = [item["name"] for item in gate_get("/options/underlyings", {})]

    def load_one(name: str) -> Tuple[str, Dict[str, Dict[str, Any]]]:
        contracts = {
            item["name"]: item
            for item in gate_get("/options/contracts", {"underlying": name})
            if item.get("is_active")
        }
        return name, contracts

    found: Dict[str, Dict[str, Dict[str, Any]]] = {}
    with ThreadPoolExecutor(max_workers=min(workers, max(len(underlyings), 1))) as executor:
        futures = {executor.submit(load_one, name): name for name in underlyings}
        for future in as_completed(futures):
            name = futures[future]
            try:
                venue_underlying, contracts = future.result()
                if contracts:
                    found[venue_underlying] = contracts
            except Exception as exc:
                print(f"Gate {name}: 合约目录跳过（{exc}）", file=sys.stderr)
    return found


def fetch_catalog(
    venue: str,
    pacer: RequestPacer,
    workers: int,
    underlying: str | None,
) -> Dict[str, Any]:
    if venue == "derive":
        return fetch_derive_catalog(pacer, workers, underlying)
    if venue == "bybit":
        return fetch_bybit_catalog(workers, underlying)
    if venue == "gate":
        return fetch_gate_catalog(workers, underlying)
    raise ValueError(f"未知交易所：{venue}")


def load_derive_snapshot(
    catalog: Dict[str, Dict[str, Dict[str, Any]]],
    pacer: RequestPacer,
    workers: int,
) -> Tuple[List[core.Node], int]:
    now = time.time()
    nodes: List[core.Node] = []
    active = 0
    for coin, options in catalog.items():
        current = {
            name: meta
            for name, meta in options.items()
            if int(meta["option_details"]["expiry"]) > now
        }
        if current:
            nodes.extend(derive_parse_tickers(coin, current, pacer, workers))
            active += len(current)
    return nodes, active


def load_bybit_snapshot(
    catalog: Dict[str, Dict[str, Dict[str, Any]]],
    workers: int,
) -> Tuple[List[core.Node], int]:
    now = time.time()

    def load_coin(
        coin: str,
        instruments: Dict[str, Dict[str, Any]],
    ) -> Tuple[List[core.Node], int]:
        current = {
            symbol: meta
            for symbol, meta in instruments.items()
            if int(meta["deliveryTime"]) // 1000 > now
        }
        result = bybit_get("/v5/market/tickers", {"category": "option", "baseCoin": coin})
        tickers = {item["symbol"]: item for item in result["list"]}
        nodes: List[core.Node] = []
        for symbol, meta in current.items():
            ticker = tickers.get(symbol)
            if not ticker:
                continue
            expiry_ts = int(meta["deliveryTime"]) // 1000
            nodes.append(
                core.Node(
                    instrument=symbol,
                    underlying=coin,
                    expiry=datetime.fromtimestamp(expiry_ts, tz=timezone.utc).strftime("%Y%m%d"),
                    expiry_ts=expiry_ts,
                    strike=bybit_strike(symbol),
                    option_type="C" if str(meta["optionsType"]).lower() == "call" else "P",
                    tick=float(meta["priceFilter"]["tickSize"]),
                    bid=fnum(ticker.get("bid1Price")),
                    bid_size=fnum(ticker.get("bid1Size")),
                    ask=fnum(ticker.get("ask1Price")),
                    ask_size=fnum(ticker.get("ask1Size")),
                    mark=fnum(ticker.get("markPrice")),
                    bid_iv=fnum(ticker.get("bid1Iv")),
                    ask_iv=fnum(ticker.get("ask1Iv")),
                    mark_iv=fnum(ticker.get("markIv")),
                    forward=fnum(ticker.get("underlyingPrice")),
                    discount=1.0,
                )
            )
        return nodes, len(current)

    nodes: List[core.Node] = []
    active = 0
    with ThreadPoolExecutor(max_workers=min(workers, max(len(catalog), 1))) as executor:
        futures = {
            executor.submit(load_coin, coin, instruments): coin
            for coin, instruments in catalog.items()
        }
        for future in as_completed(futures):
            coin = futures[future]
            try:
                current, count = future.result()
                nodes.extend(current)
                active += count
            except Exception as exc:
                print(f"Bybit {coin}: 行情跳过（{exc}）", file=sys.stderr)
    return nodes, active


def load_gate_snapshot(
    catalog: Dict[str, Dict[str, Dict[str, Any]]],
    workers: int,
) -> Tuple[List[core.Node], int]:
    now = time.time()

    def load_underlying(
        underlying: str,
        contracts: Dict[str, Dict[str, Any]],
    ) -> Tuple[List[core.Node], int]:
        current = {
            name: meta
            for name, meta in contracts.items()
            if int(meta["expiration_time"]) > now
        }
        tickers = {
            item["name"]: item
            for item in gate_get("/options/tickers", {"underlying": underlying})
        }
        nodes: List[core.Node] = []
        for name, meta in current.items():
            ticker = tickers.get(name)
            if not ticker:
                continue
            expiry_ts = int(meta["expiration_time"])
            nodes.append(
                core.Node(
                    instrument=name,
                    underlying=underlying,
                    expiry=datetime.fromtimestamp(expiry_ts, tz=timezone.utc).strftime("%Y%m%d"),
                    expiry_ts=expiry_ts,
                    strike=float(meta["strike_price"]),
                    option_type="C" if meta["is_call"] else "P",
                    tick=float(meta["order_price_round"]),
                    bid=fnum(ticker.get("bid1_price")),
                    bid_size=fnum(ticker.get("bid1_size")),
                    ask=fnum(ticker.get("ask1_price")),
                    ask_size=fnum(ticker.get("ask1_size")),
                    mark=fnum(ticker.get("mark_price")),
                    bid_iv=fnum(ticker.get("bid_iv")),
                    ask_iv=fnum(ticker.get("ask_iv")),
                    mark_iv=fnum(ticker.get("mark_iv")),
                    forward=fnum(ticker.get("underlying_price")),
                    discount=1.0,
                )
            )
        return nodes, len(current)

    nodes: List[core.Node] = []
    active = 0
    with ThreadPoolExecutor(max_workers=min(workers, max(len(catalog), 1))) as executor:
        futures = {
            executor.submit(load_underlying, name, contracts): name
            for name, contracts in catalog.items()
        }
        for future in as_completed(futures):
            name = futures[future]
            try:
                current, count = future.result()
                nodes.extend(current)
                active += count
            except Exception as exc:
                print(f"Gate {name}: 行情跳过（{exc}）", file=sys.stderr)
    calibrated, calibrations = core.calibrate_expiry_carry(nodes)
    print(f"Gate.io: Put-Call Parity 已校准 {len(calibrations)} 个到期曲面", file=sys.stderr)
    return calibrated, active


def load_snapshot(
    venue: str,
    catalog: Dict[str, Any],
    pacer: RequestPacer,
    workers: int,
) -> Tuple[List[core.Node], int, float]:
    started = time.time()
    if venue == "derive":
        nodes, active = load_derive_snapshot(catalog, pacer, workers)
    elif venue == "bybit":
        nodes, active = load_bybit_snapshot(catalog, workers)
    elif venue == "gate":
        nodes, active = load_gate_snapshot(catalog, workers)
    else:
        raise ValueError(f"未知交易所：{venue}")
    return nodes, active, time.time() - started
