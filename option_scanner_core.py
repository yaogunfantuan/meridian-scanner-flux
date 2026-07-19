#!/usr/bin/env python3
"""Venue-neutral option surface, parity, mark-regime, and P1 signal logic.

For every expiry it builds a reference smile from liquid two-sided quotes,
removes the candidate strike, and estimates a leave-one-strike-out local IV.
Mark is retained as a secondary diagnostic rather than the primary trigger.

Exchange API access and process orchestration live in separate modules.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, replace
from statistics import median
from typing import Any, Dict, List, Sequence, Tuple


YEAR_SECONDS = 365.25 * 24 * 60 * 60


@dataclass(frozen=True)
class Node:
    instrument: str
    underlying: str
    expiry: str
    expiry_ts: int
    strike: float
    option_type: str
    tick: float
    bid: float | None
    bid_size: float | None
    ask: float | None
    ask_size: float | None
    mark: float | None
    bid_iv: float | None
    ask_iv: float | None
    mark_iv: float | None
    forward: float | None
    discount: float
    index_price: float | None = None
    contract_multiplier: float = 1.0
    taker_fee_rate: float | None = None
    trade_fee_cap_rate: float | None = None
    settlement_fee_rate: float | None = None
    settlement_fee_cap_rate: float | None = None
    initial_margin_low: float | None = None
    initial_margin_high: float | None = None

    @property
    def valid_bid(self) -> bool:
        return bool(self.bid and self.bid_size and self.bid > 0 and self.bid_size > 0)

    @property
    def valid_ask(self) -> bool:
        return bool(self.ask and self.ask_size and self.ask > 0 and self.ask_size > 0)

    @property
    def mid_iv(self) -> float | None:
        if (
            self.valid_bid
            and self.valid_ask
            and self.bid_iv is not None
            and self.ask_iv is not None
            and self.bid_iv > 0
            and self.ask_iv >= self.bid_iv
        ):
            return (self.bid_iv + self.ask_iv) / 2
        return None

    @property
    def iv_width(self) -> float | None:
        if self.mid_iv is None:
            return None
        return self.ask_iv - self.bid_iv  # type: ignore[operator]

    @property
    def reference_depth(self) -> float:
        if not self.valid_bid or not self.valid_ask:
            return 0.0
        return min(self.bid_size or 0.0, self.ask_size or 0.0)


@dataclass(frozen=True)
class ReferencePoint:
    strike: float
    x: float
    iv: float
    iv_width: float
    depth: float
    option_type: str


@dataclass
class QuoteState:
    price: float
    since: float
    start_forward: float | None


def calibrate_expiry_carry(
    nodes: Sequence[Node],
    min_pairs: int = 4,
) -> Tuple[List[Node], Dict[Tuple[str, str], Dict[str, float]]]:
    """Infer discount D and forward F robustly from C-P = D(F-K)."""
    by_contract = {
        (node.underlying, node.expiry, node.strike, node.option_type): node
        for node in nodes
    }
    by_expiry: Dict[Tuple[str, str], List[Tuple[float, float, float]]] = {}
    for underlying, expiry, strike, option_type in by_contract:
        if option_type != "C":
            continue
        call = by_contract[(underlying, expiry, strike, "C")]
        put = by_contract.get((underlying, expiry, strike, "P"))
        if (
            put is None
            or not call.valid_bid
            or not call.valid_ask
            or not put.valid_bid
            or not put.valid_ask
            or call.bid is None
            or call.ask is None
            or put.bid is None
            or put.ask is None
        ):
            continue
        call_mid = (call.bid + call.ask) / 2
        put_mid = (put.bid + put.ask) / 2
        combined_half_width = ((call.ask - call.bid) + (put.ask - put.bid)) / 2
        by_expiry.setdefault((underlying, expiry), []).append(
            (strike, call_mid - put_mid, max(combined_half_width, call.tick, put.tick))
        )

    calibrations: Dict[Tuple[str, str], Dict[str, float]] = {}
    for key, pairs in by_expiry.items():
        if len(pairs) < min_pairs:
            continue
        slopes = [
            (diff_j - diff_i) / (strike_j - strike_i)
            for index, (strike_i, diff_i, _width_i) in enumerate(pairs)
            for strike_j, diff_j, _width_j in pairs[index + 1 :]
            if strike_j != strike_i
        ]
        if not slopes:
            continue
        discount = -median(slopes)
        if not 0.8 <= discount <= 1.2:
            continue
        forwards = [strike + difference / discount for strike, difference, _ in pairs]
        forward = median(forwards)
        if forward <= 0:
            continue
        residuals = [
            abs(difference - discount * (forward - strike))
            for strike, difference, _width in pairs
        ]
        calibrations[key] = {
            "forward": forward,
            "discount": discount,
            "pairs": float(len(pairs)),
            "residual": median(residuals),
            "width": median(width for _strike, _difference, width in pairs),
        }

    calibrated = [
        replace(
            node,
            forward=calibrations[(node.underlying, node.expiry)]["forward"],
            discount=calibrations[(node.underlying, node.expiry)]["discount"],
        )
        if (node.underlying, node.expiry) in calibrations
        else node
        for node in nodes
    ]
    return calibrated, calibrations


def build_reference_smile(
    nodes: Sequence[Node],
    max_iv_width: float,
    min_depth: float,
) -> List[ReferencePoint]:
    """Choose one preferably-OTM, liquid reference quote at each strike."""
    by_strike: Dict[float, List[Node]] = defaultdict(list)
    for node in nodes:
        if (
            node.forward
            and node.forward > 0
            and node.mid_iv is not None
            and node.iv_width is not None
            and node.iv_width <= max_iv_width
            and node.reference_depth >= min_depth
        ):
            by_strike[node.strike].append(node)

    points: List[ReferencePoint] = []
    for strike, choices in by_strike.items():
        forward = choices[0].forward or 0.0
        preferred_type = "P" if strike < forward else "C"
        choices.sort(
            key=lambda node: (
                node.option_type != preferred_type,
                node.iv_width or float("inf"),
                -node.reference_depth,
            )
        )
        chosen = choices[0]
        points.append(
            ReferencePoint(
                strike=strike,
                x=math.log(strike / (chosen.forward or strike)),
                iv=chosen.mid_iv or 0.0,
                iv_width=chosen.iv_width or 0.0,
                depth=chosen.reference_depth,
                option_type=chosen.option_type,
            )
        )
    return sorted(points, key=lambda point: point.x)


def weighted_local_fit(
    x0: float,
    points: Sequence[ReferencePoint],
    neighbors_each_side: int,
    min_neighbors: int,
    allow_one_sided: bool,
    max_neighbor_distance: float | None = None,
    max_bracket_span: float | None = None,
) -> Tuple[float, float, List[ReferencePoint]] | None:
    left = sorted((point for point in points if point.x < x0), key=lambda p: x0 - p.x)
    right = sorted((point for point in points if point.x > x0), key=lambda p: p.x - x0)
    if max_neighbor_distance is not None:
        left = [point for point in left if x0 - point.x <= max_neighbor_distance]
        right = [point for point in right if point.x - x0 <= max_neighbor_distance]
    if not allow_one_sided and (not left or not right):
        return None
    if (
        left
        and right
        and max_bracket_span is not None
        and right[0].x - left[0].x > max_bracket_span
    ):
        return None
    selected = left[:neighbors_each_side] + right[:neighbors_each_side]
    if len(selected) < min_neighbors:
        if not allow_one_sided:
            return None
        eligible = points
        if max_neighbor_distance is not None:
            eligible = [
                point for point in points
                if abs(point.x - x0) <= max_neighbor_distance
            ]
        selected = sorted(eligible, key=lambda point: abs(point.x - x0))[:min_neighbors]
    if len(selected) < 2:
        return None

    distances = [abs(point.x - x0) for point in selected if point.x != x0]
    scale = median(distances) if distances else 0.01
    scale = max(scale, 1e-4)

    weighted: List[Tuple[ReferencePoint, float]] = []
    for point in selected:
        distance_weight = 1 / (abs(point.x - x0) + 0.25 * scale)
        width_weight = 1 / max(point.iv_width, 0.005)
        depth_weight = 1 + min(math.log1p(point.depth), 4.0) / 4
        weighted.append((point, distance_weight * width_weight * depth_weight))

    sw = sum(weight for _, weight in weighted)
    sx = sum(weight * (point.x - x0) for point, weight in weighted)
    sy = sum(weight * point.iv for point, weight in weighted)
    sxx = sum(weight * (point.x - x0) ** 2 for point, weight in weighted)
    sxy = sum(weight * (point.x - x0) * point.iv for point, weight in weighted)
    determinant = sw * sxx - sx * sx
    if abs(determinant) < 1e-14:
        local_iv = sy / sw
        slope = 0.0
    else:
        local_iv = (sy * sxx - sx * sxy) / determinant
        slope = (sw * sxy - sx * sy) / determinant

    residuals = [abs(point.iv - (local_iv + slope * (point.x - x0))) for point in selected]
    noise = max(1.4826 * median(residuals), 0.0025)
    return local_iv, noise, sorted(selected, key=lambda point: point.x)


def normal_cdf(value: float) -> float:
    return 0.5 * (1 + math.erf(value / math.sqrt(2)))


def black_forward_price(
    forward: float,
    strike: float,
    years: float,
    iv: float,
    discount: float,
    option_type: str,
) -> float:
    if years <= 0 or iv <= 0:
        intrinsic = max(forward - strike, 0.0) if option_type == "C" else max(strike - forward, 0.0)
        return discount * intrinsic
    sigma_root_t = iv * math.sqrt(years)
    d1 = (math.log(forward / strike) + 0.5 * iv * iv * years) / sigma_root_t
    d2 = d1 - sigma_root_t
    if option_type == "C":
        return discount * (forward * normal_cdf(d1) - strike * normal_cdf(d2))
    return discount * (strike * normal_cdf(-d2) - forward * normal_cdf(-d1))


def black_forward_vega(
    forward: float,
    strike: float,
    years: float,
    iv: float,
    discount: float,
) -> float:
    if years <= 0 or iv <= 0 or forward <= 0 or strike <= 0:
        return 0.0
    sigma_root_t = iv * math.sqrt(years)
    d1 = (math.log(forward / strike) + 0.5 * iv * iv * years) / sigma_root_t
    density = math.exp(-0.5 * d1 * d1) / math.sqrt(2 * math.pi)
    return discount * forward * density * math.sqrt(years)


def option_trade_fee_per_underlying(node: Node, price: float | None) -> float | None:
    """Return taker fee in quote currency per one unit of underlying."""
    if (
        price is None
        or price < 0
        or node.index_price is None
        or node.index_price <= 0
        or node.taker_fee_rate is None
        or node.taker_fee_rate < 0
    ):
        return None
    fee = node.taker_fee_rate * node.index_price
    if node.trade_fee_cap_rate is not None and node.trade_fee_cap_rate >= 0:
        fee = min(fee, node.trade_fee_cap_rate * price)
    return max(fee, 0.0)


def option_settlement_fee_per_underlying(node: Node) -> float | None:
    """Estimate expiry fee at the calibrated forward; exactly one C/P leg is ITM."""
    if (
        node.forward is None
        or node.forward <= 0
        or node.settlement_fee_rate is None
        or node.settlement_fee_rate < 0
    ):
        return None
    intrinsic = (
        max(node.forward - node.strike, 0.0)
        if node.option_type == "C"
        else max(node.strike - node.forward, 0.0)
    )
    if intrinsic <= 0:
        return 0.0
    fee = node.settlement_fee_rate * node.forward
    if node.settlement_fee_cap_rate is not None and node.settlement_fee_cap_rate >= 0:
        fee = min(fee, node.settlement_fee_cap_rate * intrinsic)
    return max(fee, 0.0)


def short_option_initial_margin(node: Node, size: float) -> float | None:
    """Estimate regular-margin initial margin for Gate-style linear options."""
    if (
        node.index_price is None
        or node.index_price <= 0
        or node.mark is None
        or node.mark < 0
        or node.initial_margin_low is None
        or node.initial_margin_high is None
        or node.contract_multiplier <= 0
        or size <= 0
    ):
        return None
    spot = node.index_price
    if node.option_type == "C":
        otm = max(node.strike - spot, 0.0)
        risk = max(
            node.initial_margin_low * spot,
            node.initial_margin_high * spot - otm,
        )
    else:
        otm = max(spot - node.strike, 0.0)
        risk = max(
            node.initial_margin_low * spot * (1 + node.mark / spot),
            node.initial_margin_high * spot - otm,
        )
    return (risk + node.mark) * size * node.contract_multiplier


def executable_parity_metrics(
    node: Node,
    signal: str,
    nodes_by_key: Dict[Tuple[str, str, float, str], Node],
    hedge_taker_rate: float,
    hedge_leverage: float,
) -> Dict[str, Any] | None:
    """Return raw and fee-adjusted executable C/P parity economics.

    The conservative reversion case assumes taker execution for opening and
    closing both option legs plus opening and closing a one-delta hedge.
    Funding is deliberately excluded because its future path is not locked.
    """
    if not node.forward or node.forward <= 0 or node.tick <= 0:
        return None
    other_type = "P" if node.option_type == "C" else "C"
    other = nodes_by_key.get((node.underlying, node.expiry, node.strike, other_type))
    if other is None:
        return None
    parity_value = node.discount * (node.forward - node.strike)

    if node.option_type == "C" and signal == "BUY":
        if not node.valid_ask or not other.valid_bid or node.ask is None or other.bid is None:
            return None
        edge = parity_value - (node.ask - other.bid)
        size = min(node.ask_size or 0.0, other.bid_size or 0.0)
        long_node, long_price = node, node.ask
        short_node, short_price = other, other.bid
    elif node.option_type == "C" and signal == "SELL":
        if not node.valid_bid or not other.valid_ask or node.bid is None or other.ask is None:
            return None
        edge = (node.bid - other.ask) - parity_value
        size = min(node.bid_size or 0.0, other.ask_size or 0.0)
        long_node, long_price = other, other.ask
        short_node, short_price = node, node.bid
    elif node.option_type == "P" and signal == "BUY":
        if not node.valid_ask or not other.valid_bid or node.ask is None or other.bid is None:
            return None
        edge = (other.bid - node.ask) - parity_value
        size = min(node.ask_size or 0.0, other.bid_size or 0.0)
        long_node, long_price = node, node.ask
        short_node, short_price = other, other.bid
    else:  # SELL put
        if not node.valid_bid or not other.valid_ask or node.bid is None or other.ask is None:
            return None
        edge = parity_value - (other.ask - node.bid)
        size = min(node.bid_size or 0.0, other.ask_size or 0.0)
        long_node, long_price = other, other.ask
        short_node, short_price = node, node.bid

    multiplier = node.contract_multiplier
    if multiplier <= 0 or not math.isclose(
        multiplier, other.contract_multiplier, rel_tol=1e-9, abs_tol=1e-12
    ):
        return None
    quantity = size * multiplier
    raw_ticks = edge / node.tick
    raw_usdt = edge * quantity

    open_long_fee = option_trade_fee_per_underlying(long_node, long_price)
    open_short_fee = option_trade_fee_per_underlying(short_node, short_price)
    close_long_fee = option_trade_fee_per_underlying(long_node, long_node.bid)
    close_short_fee = option_trade_fee_per_underlying(short_node, short_node.ask)
    option_fees = (open_long_fee, open_short_fee, close_long_fee, close_short_fee)
    option_roundtrip_fee = sum(option_fees) if all(fee is not None for fee in option_fees) else None

    index_price = node.index_price or other.index_price
    hedge_roundtrip_fee = (
        2 * hedge_taker_rate * index_price
        if index_price and index_price > 0 and hedge_taker_rate >= 0
        else None
    )
    fully_costed = option_roundtrip_fee is not None and hedge_roundtrip_fee is not None
    roundtrip_cost = (
        option_roundtrip_fee + hedge_roundtrip_fee  # type: ignore[operator]
        if fully_costed
        else None
    )
    net_points = edge - roundtrip_cost if roundtrip_cost is not None else None

    settlement_node = long_node if (
        (long_node.option_type == "C" and node.forward >= node.strike)
        or (long_node.option_type == "P" and node.forward < node.strike)
    ) else short_node
    settlement_fee = option_settlement_fee_per_underlying(settlement_node)
    entry_option_fee = (
        open_long_fee + open_short_fee
        if open_long_fee is not None and open_short_fee is not None
        else None
    )
    expiry_net_points = (
        edge - entry_option_fee - settlement_fee
        if entry_option_fee is not None and settlement_fee is not None
        else None
    )

    short_margin = short_option_initial_margin(short_node, size)
    pair_debit = max(long_price - short_price, 0.0) * quantity
    hedge_margin = (
        index_price * quantity / hedge_leverage
        if index_price and index_price > 0 and hedge_leverage > 0
        else None
    )
    capital = (
        pair_debit + short_margin + hedge_margin
        if short_margin is not None and hedge_margin is not None
        else None
    )
    net_usdt = net_points * quantity if net_points is not None else None
    return {
        "raw_ticks": raw_ticks,
        "raw_usdt": raw_usdt,
        "size": size,
        "quantity": quantity,
        "option_roundtrip_fee_ticks": (
            option_roundtrip_fee / node.tick if option_roundtrip_fee is not None else None
        ),
        "hedge_roundtrip_fee_ticks": (
            hedge_roundtrip_fee / node.tick if hedge_roundtrip_fee is not None else None
        ),
        "net_ticks": net_points / node.tick if net_points is not None else None,
        "net_usdt": net_usdt,
        "expiry_net_usdt": (
            expiry_net_points * quantity if expiry_net_points is not None else None
        ),
        "capital_est_usdt": capital,
        "return_bps": (
            net_usdt / capital * 10_000
            if net_usdt is not None and capital is not None and capital > 0
            else None
        ),
        "fees_complete": fully_costed,
    }


def pair_mid_iv(
    node: Node,
    nodes_by_key: Dict[Tuple[str, str, float, str], Node],
) -> float | None:
    other_type = "P" if node.option_type == "C" else "C"
    other = nodes_by_key.get((node.underlying, node.expiry, node.strike, other_type))
    return other.mid_iv if other else None


def local_quality_flags(
    node: Node,
    signal: str,
    quote_iv: float,
    paired_iv: float | None,
    pair_iv_tolerance: float,
    min_exit_depth: float,
) -> List[str]:
    """Return non-blocking warnings for a single-leg mean-reversion signal."""
    flags: List[str] = []
    if paired_iv is not None:
        pair_conflicts = (
            signal == "BUY" and paired_iv < quote_iv - pair_iv_tolerance
        ) or (
            signal == "SELL" and paired_iv > quote_iv + pair_iv_tolerance
        )
        if pair_conflicts:
            flags.append("PAIR_CONFLICT")

    exit_depth = node.bid_size if signal == "BUY" else node.ask_size
    if exit_depth is None or exit_depth < min_exit_depth:
        flags.append("EXIT_THIN")
    return flags


def neighbor_text(points: Sequence[ReferencePoint]) -> str:
    return ",".join(
        f"{point.option_type}{point.strike:g}:{point.iv * 100:.2f}%"
        for point in points
    )


def update_quote_states(
    nodes: Sequence[Node],
    states: Dict[Tuple[str, str], QuoteState],
    now: float,
) -> None:
    seen: set[Tuple[str, str]] = set()
    for node in nodes:
        for side, valid, price in (
            ("BUY", node.valid_ask, node.ask),
            ("SELL", node.valid_bid, node.bid),
        ):
            if not valid or price is None:
                continue
            key = (node.instrument, side)
            seen.add(key)
            state = states.get(key)
            if state is None or state.price != price:
                states[key] = QuoteState(price=price, since=now, start_forward=node.forward)
    for key in list(states):
        if key not in seen:
            del states[key]


def mark_bias_contexts(
    nodes: Sequence[Node],
    max_iv_width: float,
    min_depth: float,
) -> Dict[Tuple[str, str], Tuple[float, float, int]]:
    """Estimate each expiry's systematic mark-IV bias versus executable mids."""
    grouped: Dict[Tuple[str, str], List[float]] = defaultdict(list)
    for node in nodes:
        if (
            node.mid_iv is None
            or node.mark_iv is None
            or node.mark_iv <= 0
            or node.iv_width is None
            or node.iv_width > max_iv_width
            or node.reference_depth < min_depth
        ):
            continue
        grouped[(node.underlying, node.expiry)].append(node.mark_iv - node.mid_iv)

    contexts: Dict[Tuple[str, str], Tuple[float, float, int]] = {}
    for key, biases in grouped.items():
        center = median(biases)
        deviations = [abs(value - center) for value in biases]
        noise = max(1.4826 * median(deviations), 0.01)
        contexts[key] = (center, noise, len(biases))
    return contexts


def find_mark_regimes(
    nodes: Sequence[Node],
    args: Any,
) -> List[Dict[str, Any]]:
    contexts = mark_bias_contexts(
        nodes, args.max_reference_iv_width, args.min_reference_depth
    )
    rows: List[Dict[str, Any]] = []
    for (underlying, expiry), (bias, noise, count) in contexts.items():
        if count < args.mark_regime_min_points or abs(bias) < args.mark_regime_min_bias:
            continue
        rows.append(
            {
                "underlying": underlying,
                "expiry": expiry,
                "points": count,
                "bias_pp": bias * 100,
                "noise_pp": noise * 100,
                "direction": "MARK_HIGH" if bias > 0 else "MARK_LOW",
            }
        )
    return sorted(rows, key=lambda row: abs(row["bias_pp"]), reverse=True)


def find_candidates(
    nodes: Sequence[Node],
    states: Dict[Tuple[str, str], QuoteState],
    args: Any,
    now: float,
) -> List[Dict[str, Any]]:
    by_expiry: Dict[Tuple[str, str], List[Node]] = defaultdict(list)
    for node in nodes:
        by_expiry[(node.underlying, node.expiry)].append(node)
    nodes_by_key = {
        (node.underlying, node.expiry, node.strike, node.option_type): node
        for node in nodes
    }
    candidates: List[Dict[str, Any]] = []
    fit_info: Dict[str, Tuple[float, List[ReferencePoint]] | None] = {}

    for (_underlying, _expiry), expiry_nodes in by_expiry.items():
        reference = build_reference_smile(
            expiry_nodes,
            max_iv_width=args.max_reference_iv_width,
            min_depth=args.min_reference_depth,
        )
        for node in expiry_nodes:
            if not node.forward or node.forward <= 0 or node.tick <= 0:
                continue
            x0 = math.log(node.strike / node.forward)
            # Leave the entire strike out, including the opposite option type.
            leave_one_out = [point for point in reference if point.strike != node.strike]
            fit = weighted_local_fit(
                x0,
                leave_one_out,
                neighbors_each_side=args.neighbors,
                min_neighbors=args.min_neighbors,
                allow_one_sided=args.allow_one_sided,
                max_neighbor_distance=args.max_neighbor_log_distance,
                max_bracket_span=args.max_bracket_log_span,
            )
            if fit is None:
                fit_info[node.instrument] = None
                continue
            local_iv, local_noise, neighbors = fit
            fit_info[node.instrument] = (local_iv, neighbors)
            if not 0 < local_iv < 5:
                continue
            years = max((node.expiry_ts - now) / YEAR_SECONDS, 0.0)
            fair_price = black_forward_price(
                node.forward,
                node.strike,
                years,
                local_iv,
                node.discount,
                node.option_type,
            )
            own_half_width = (node.iv_width or 0.0) / 2
            denominator = max(local_noise, own_half_width, args.z_floor)

            sides = (
                ("BUY", node.valid_ask, node.ask, node.ask_size, node.ask_iv, local_iv - (node.ask_iv or 0.0), fair_price - (node.ask or 0.0)),
                ("SELL", node.valid_bid, node.bid, node.bid_size, node.bid_iv, (node.bid_iv or 0.0) - local_iv, (node.bid or 0.0) - fair_price),
            )
            for signal, valid, price, size, quote_iv, iv_edge, price_edge in sides:
                if (
                    not valid
                    or price is None
                    or size is None
                    or quote_iv is None
                    or quote_iv <= 0
                    or size < args.min_size
                    or iv_edge < args.min_iv_edge
                ):
                    continue
                zscore = iv_edge / denominator
                gross_edge_ticks = price_edge / node.tick
                open_fee = option_trade_fee_per_underlying(node, price)
                single_fee_ticks = (
                    2 * open_fee / node.tick
                    if open_fee is not None
                    else None
                )
                net_edge_ticks = gross_edge_ticks
                if single_fee_ticks is not None:
                    net_edge_ticks -= single_fee_ticks
                if zscore < args.min_z or net_edge_ticks < args.min_edge_ticks:
                    continue
                parity = executable_parity_metrics(
                    node,
                    signal,
                    nodes_by_key,
                    hedge_taker_rate=args.hedge_taker_rate,
                    hedge_leverage=args.hedge_leverage,
                )
                parity_edge_ticks = parity["raw_ticks"] if parity else None
                parity_size = parity["size"] if parity else None
                vega_ticks_per_pp = (
                    black_forward_vega(
                        node.forward, node.strike, years, local_iv, node.discount
                    )
                    * 0.01
                    / node.tick
                )
                intrinsic = node.discount * (
                    max(node.forward - node.strike, 0.0)
                    if node.option_type == "C"
                    else max(node.strike - node.forward, 0.0)
                )
                state = states.get((node.instrument, signal))
                unchanged_seconds = max(now - state.since, 0.0) if state else 0.0
                forward_move_bps = 0.0
                if state and state.start_forward and node.forward:
                    forward_move_bps = (node.forward / state.start_forward - 1) * 10_000
                score = zscore * math.sqrt(max(net_edge_ticks, 0.0)) * math.log1p(size)
                pair_iv = pair_mid_iv(node, nodes_by_key)
                quality_flags = local_quality_flags(
                    node,
                    signal,
                    quote_iv,
                    pair_iv,
                    args.pair_iv_tolerance,
                    args.min_exit_depth,
                )
                left_neighbors = [point for point in neighbors if point.x < x0]
                right_neighbors = [point for point in neighbors if point.x > x0]
                bracket_log_span = (
                    min(point.x for point in right_neighbors)
                    - max(point.x for point in left_neighbors)
                    if left_neighbors and right_neighbors
                    else None
                )
                single_quantity = size * node.contract_multiplier
                single_gross_usdt = gross_edge_ticks * node.tick * single_quantity
                single_fee_usdt = (
                    single_fee_ticks * node.tick * single_quantity
                    if single_fee_ticks is not None
                    else None
                )
                single_net_usdt = net_edge_ticks * node.tick * single_quantity
                single_capital = (
                    price * single_quantity
                    if signal == "BUY"
                    else short_option_initial_margin(node, size)
                )
                candidates.append(
                    {
                        "instrument": node.instrument,
                        "signal": signal,
                        "basis": "LOCAL",
                        "price": price,
                        "size": size,
                        "bid": node.bid,
                        "ask": node.ask,
                        "quote_iv": quote_iv,
                        "local_iv": local_iv,
                        "iv_edge_pp": iv_edge * 100,
                        "local_z": zscore,
                        "fair_price": fair_price,
                        "gross_edge_ticks": gross_edge_ticks,
                        "net_edge_ticks": net_edge_ticks,
                        "single_fee_ticks": single_fee_ticks,
                        "single_gross_usdt": single_gross_usdt,
                        "single_fee_usdt": single_fee_usdt,
                        "single_net_usdt": single_net_usdt,
                        "single_capital_est_usdt": single_capital,
                        "single_return_bps": (
                            single_net_usdt / single_capital * 10_000
                            if single_capital is not None and single_capital > 0
                            else None
                        ),
                        "parity_edge_ticks": parity_edge_ticks,
                        "parity_net_ticks": parity["net_ticks"] if parity else None,
                        "parity_raw_usdt": parity["raw_usdt"] if parity else None,
                        "parity_net_usdt": parity["net_usdt"] if parity else None,
                        "parity_expiry_net_usdt": parity["expiry_net_usdt"] if parity else None,
                        "parity_option_fee_ticks": (
                            parity["option_roundtrip_fee_ticks"] if parity else None
                        ),
                        "parity_hedge_fee_ticks": (
                            parity["hedge_roundtrip_fee_ticks"] if parity else None
                        ),
                        "parity_capital_est_usdt": (
                            parity["capital_est_usdt"] if parity else None
                        ),
                        "parity_return_bps": parity["return_bps"] if parity else None,
                        "parity_fees_complete": parity["fees_complete"] if parity else False,
                        "parity_size": parity_size,
                        "vega_ticks_per_pp": vega_ticks_per_pp,
                        "time_value": price - intrinsic,
                        "forward": node.forward,
                        "discount": node.discount,
                        "pair_iv": pair_iv,
                        "quality": "+".join(quality_flags) if quality_flags else "OK",
                        "quality_flags": quality_flags,
                        "exit_depth": node.bid_size if signal == "BUY" else node.ask_size,
                        "neighbor_max_log_distance": max(
                            abs(point.x - x0) for point in neighbors
                        ),
                        "bracket_log_span": bracket_log_span,
                        "mark": node.mark,
                        "mark_iv": node.mark_iv,
                        "unchanged_s": unchanged_seconds,
                        "forward_move_bps": forward_move_bps,
                        "score": score,
                        "neighbors": neighbor_text(neighbors),
                    }
                )

    # Mark is diagnostic, not the primary fair value.  Remove each expiry's
    # systematic mark-IV bias before calling a single contract an outlier.
    mark_contexts = mark_bias_contexts(
        nodes, args.max_reference_iv_width, args.min_reference_depth
    )
    existing = {(row["instrument"], row["signal"]) for row in candidates}
    for node in nodes:
        if (
            node.mark is None
            or node.mark <= 0
            or node.mark_iv is None
            or node.mark_iv <= 0
            or node.tick <= 0
        ):
            continue
        context = mark_contexts.get((node.underlying, node.expiry))
        context_ready = bool(context and context[2] >= args.mark_regime_min_points)
        mark_bias = context[0] if context_ready and context else 0.0
        mark_noise = context[1] if context_ready and context else max(args.z_floor, 0.01)
        adjusted_mark_iv = node.mark_iv - mark_bias
        if node.forward and node.forward > 0:
            years = max((node.expiry_ts - now) / YEAR_SECONDS, 0.0)
            adjusted_mark_price = black_forward_price(
                node.forward,
                node.strike,
                years,
                adjusted_mark_iv,
                node.discount,
                node.option_type,
            )
        else:
            adjusted_mark_price = node.mark

        mark_sides = (
            (
                "BUY", node.valid_ask, node.ask, node.ask_size, node.ask_iv,
                adjusted_mark_iv - (node.ask_iv or 0.0),
                adjusted_mark_price - (node.ask or 0.0),
            ),
            (
                "SELL", node.valid_bid, node.bid, node.bid_size, node.bid_iv,
                (node.bid_iv or 0.0) - adjusted_mark_iv,
                (node.bid or 0.0) - adjusted_mark_price,
            ),
        )
        for signal, valid, price, size, quote_iv, residual_edge, adjusted_price_edge in mark_sides:
            if (
                (node.instrument, signal) in existing
                or not valid
                or price is None
                or size is None
                or quote_iv is None
                or quote_iv <= 0
                or size < args.min_size
            ):
                continue

            if context_ready:
                basis = "MARK_OUTLIER"
                iv_edge = residual_edge
                zscore = iv_edge / max(mark_noise, args.z_floor)
                fair_price = adjusted_mark_price
                price_edge = adjusted_price_edge
                if iv_edge < args.min_iv_edge or zscore < args.mark_outlier_z:
                    continue
            else:
                # With too few clean mids, retain only a clearly labelled
                # observation so sparse-wing cases such as HYPE 75-C survive.
                basis = "MARK_FALLBACK"
                if signal == "BUY":
                    iv_edge = node.mark_iv - quote_iv
                    price_edge = node.mark - price
                else:
                    iv_edge = quote_iv - node.mark_iv
                    price_edge = price - node.mark
                if iv_edge < args.min_iv_edge:
                    continue
                zscore = iv_edge / max(args.z_floor, 0.01)
                fair_price = node.mark

            gross_edge_ticks = price_edge / node.tick
            net_edge_ticks = gross_edge_ticks - args.cost_ticks
            if (
                gross_edge_ticks < args.mark_fallback_ticks
                or net_edge_ticks < args.min_edge_ticks
            ):
                continue
            state = states.get((node.instrument, signal))
            unchanged_seconds = max(now - state.since, 0.0) if state else 0.0
            forward_move_bps = 0.0
            if state and state.start_forward and node.forward:
                forward_move_bps = (node.forward / state.start_forward - 1) * 10_000
            score = zscore * math.sqrt(net_edge_ticks) * math.log1p(size)
            local_fit = fit_info.get(node.instrument)
            if basis == "MARK_OUTLIER" and context:
                fallback_detail = (
                    f"同到期日 mark bias {context[0] * 100:+.2f}pp 已扣除；"
                    f"稳健噪声 {context[1] * 100:.2f}pp，样本 {context[2]}"
                )
            elif local_fit is None:
                fallback_detail = "局部曲面及 mark regime 样本不足；仅作人工观察"
            else:
                fallback_detail = (
                    f"局部拟合 {local_fit[0] * 100:.2f}% 未形成 LOCAL 信号；"
                    "mark regime 样本不足，仅作人工观察"
                )
            candidates.append(
                {
                    "instrument": node.instrument,
                    "signal": signal,
                    "basis": basis,
                    "price": price,
                    "size": size,
                    "bid": node.bid,
                    "ask": node.ask,
                    "quote_iv": quote_iv,
                    "local_iv": adjusted_mark_iv if context_ready else node.mark_iv,
                    "iv_edge_pp": iv_edge * 100,
                    "local_z": zscore,
                    "fair_price": fair_price,
                    "gross_edge_ticks": gross_edge_ticks,
                    "net_edge_ticks": net_edge_ticks,
                    "single_fee_ticks": None,
                    "single_gross_usdt": None,
                    "single_fee_usdt": None,
                    "single_net_usdt": None,
                    "single_capital_est_usdt": None,
                    "single_return_bps": None,
                    "parity_edge_ticks": None,
                    "parity_net_ticks": None,
                    "parity_raw_usdt": None,
                    "parity_net_usdt": None,
                    "parity_expiry_net_usdt": None,
                    "parity_option_fee_ticks": None,
                    "parity_hedge_fee_ticks": None,
                    "parity_capital_est_usdt": None,
                    "parity_return_bps": None,
                    "parity_fees_complete": False,
                    "parity_size": None,
                    "vega_ticks_per_pp": None,
                    "time_value": None,
                    "forward": node.forward,
                    "discount": node.discount,
                    "pair_iv": pair_mid_iv(node, nodes_by_key),
                    "quality": "MARK_ONLY",
                    "quality_flags": [],
                    "exit_depth": None,
                    "neighbor_max_log_distance": None,
                    "bracket_log_span": None,
                    "mark": node.mark,
                    "mark_iv": node.mark_iv,
                    "unchanged_s": unchanged_seconds,
                    "forward_move_bps": forward_move_bps,
                    "score": score,
                    "neighbors": fallback_detail,
                }
            )
    basis_rank = {"LOCAL": 3, "MARK_OUTLIER": 2, "MARK_FALLBACK": 1}
    return sorted(
        candidates,
        key=lambda row: (basis_rank.get(row["basis"], 0), row["score"], row["net_edge_ticks"]),
        reverse=True,
    )


def one_tick_rows(
    nodes: Sequence[Node],
    venue: str,
    local_signals: Dict[str, str],
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for node in nodes:
        if not node.valid_bid or not node.valid_ask or node.bid is None or node.ask is None:
            continue
        spread = node.ask - node.bid
        if node.tick <= 0 or not math.isclose(
            spread, node.tick, rel_tol=1e-9, abs_tol=node.tick * 1e-6
        ):
            continue
        side = local_signals.get(node.instrument, "NEUTRAL")
        mark_gap = 0.0
        if node.mark is not None and node.mark > 0:
            mark_gap = max(abs(node.mark - node.bid), abs(node.mark - node.ask)) / node.tick
        rows.append(
            {
                "venue": venue,
                "instrument": node.instrument,
                "bid": node.bid,
                "bid_size": node.bid_size,
                "ask": node.ask,
                "ask_size": node.ask_size,
                "mark": node.mark,
                "tick": node.tick,
                "side": side,
                "confirmation": "LOCAL" if side != "NEUTRAL" else "-",
                "mark_gap": mark_gap,
                "depth": min(node.bid_size or 0.0, node.ask_size or 0.0),
            }
        )
    return sorted(
        rows,
        key=lambda row: (row["confirmation"] == "LOCAL", row["depth"], row["instrument"]),
        reverse=True,
    )


def is_local_net(row: Dict[str, Any], args: Any) -> bool:
    return bool(
        row.get("basis") == "LOCAL"
        and row.get("net_edge_ticks") is not None
        and row["net_edge_ticks"] >= args.min_edge_ticks
        and row.get("size") is not None
        and row["size"] >= args.min_size
        and row.get("vega_ticks_per_pp") is not None
        and row["vega_ticks_per_pp"] >= args.min_vega_ticks_per_pp
    )


def is_pcp_net(row: Dict[str, Any], args: Any) -> bool:
    return bool(
        is_local_net(row, args)
        and row.get("parity_fees_complete")
        and row.get("parity_net_ticks") is not None
        and row["parity_net_ticks"] >= args.min_parity_edge_ticks
        and row.get("parity_size") is not None
        and row["parity_size"] >= args.min_size
    )


def is_p1_local(row: Dict[str, Any], args: Any) -> bool:
    """Backward-compatible name: P1 now includes fee-positive LOCAL reversion."""
    return is_local_net(row, args)


def text(value: Any, digits: int = 4) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.{digits}f}".rstrip("0").rstrip(".")
    return str(value)


def print_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> None:
    rendered = [[text(value) for value in row] for row in rows]
    if not rendered:
        print("未发现。")
        return
    widths = [
        max(len(headers[index]), *(len(row[index]) for row in rendered))
        for index in range(len(headers))
    ]
    print("  ".join(headers[index].ljust(widths[index]) for index in range(len(headers))))
    print("  ".join("-" * width for width in widths))
    for row in rendered:
        print("  ".join(row[index].ljust(widths[index]) for index in range(len(headers))))


def print_candidate_section(
    title: str,
    rows: Sequence[Dict[str, Any]],
    limit: int,
) -> None:
    shown = list(rows[:limit])
    print(f"\n{title}: {len(rows)}（显示前 {len(shown)}）")
    print_table(
        (
            "venue", "instrument", "mode", "quality", "signal", "1tick", "price", "size",
            "quote_iv%", "ref_iv%", "iv_edge_pp", "local_gross_$", "fee_2x_$",
            "local_net_$", "local_net_t",
            "pcp_gross_t", "pcp_net_t", "pcp_net_$", "pair_size",
            "capital_$", "ret_bp", "mark", "score",
        ),
        [
            (
                row["venue"], row["instrument"], row.get("mode", "LOCAL_NET"),
                row.get("quality", "-"), row["signal"],
                row.get("one_tick", "-"), row["price"], row["size"],
                row["quote_iv"] * 100, row["local_iv"] * 100, row["iv_edge_pp"],
                row.get("single_gross_usdt"), row.get("single_fee_usdt"),
                row.get("single_net_usdt"), row["net_edge_ticks"],
                row.get("parity_edge_ticks"), row.get("parity_net_ticks"),
                row.get("parity_net_usdt"), row.get("parity_size"),
                (
                    row.get("parity_capital_est_usdt")
                    if row.get("mode") == "PCP_NET"
                    else row.get("single_capital_est_usdt")
                ),
                (
                    row.get("parity_return_bps")
                    if row.get("mode") == "PCP_NET"
                    else row.get("single_return_bps")
                ),
                row["mark"], row["score"],
            )
            for row in shown
        ],
    )
