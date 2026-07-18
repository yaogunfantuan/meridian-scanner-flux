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
) -> Tuple[float, float, List[ReferencePoint]] | None:
    left = sorted((point for point in points if point.x < x0), key=lambda p: x0 - p.x)
    right = sorted((point for point in points if point.x > x0), key=lambda p: p.x - x0)
    if not allow_one_sided and (not left or not right):
        return None
    selected = left[:neighbors_each_side] + right[:neighbors_each_side]
    if len(selected) < min_neighbors:
        if not allow_one_sided:
            return None
        selected = sorted(points, key=lambda point: abs(point.x - x0))[:min_neighbors]
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


def executable_parity_edge(
    node: Node,
    signal: str,
    nodes_by_key: Dict[Tuple[str, str, float, str], Node],
) -> Tuple[float, float] | None:
    """Return executable C/P parity edge in candidate ticks and paired size."""
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
    elif node.option_type == "C" and signal == "SELL":
        if not node.valid_bid or not other.valid_ask or node.bid is None or other.ask is None:
            return None
        edge = (node.bid - other.ask) - parity_value
        size = min(node.bid_size or 0.0, other.ask_size or 0.0)
    elif node.option_type == "P" and signal == "BUY":
        if not node.valid_ask or not other.valid_bid or node.ask is None or other.bid is None:
            return None
        edge = (other.bid - node.ask) - parity_value
        size = min(node.ask_size or 0.0, other.bid_size or 0.0)
    else:  # SELL put
        if not node.valid_bid or not other.valid_ask or node.bid is None or other.ask is None:
            return None
        edge = parity_value - (other.ask - node.bid)
        size = min(node.bid_size or 0.0, other.ask_size or 0.0)
    return edge / node.tick, size


def pair_mid_iv(
    node: Node,
    nodes_by_key: Dict[Tuple[str, str, float, str], Node],
) -> float | None:
    other_type = "P" if node.option_type == "C" else "C"
    other = nodes_by_key.get((node.underlying, node.expiry, node.strike, other_type))
    return other.mid_iv if other else None


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
                net_edge_ticks = gross_edge_ticks - args.cost_ticks
                if zscore < args.min_z or net_edge_ticks < args.min_edge_ticks:
                    continue
                parity = executable_parity_edge(node, signal, nodes_by_key)
                parity_edge_ticks = parity[0] if parity else None
                parity_size = parity[1] if parity else None
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
                        "parity_edge_ticks": parity_edge_ticks,
                        "parity_size": parity_size,
                        "vega_ticks_per_pp": vega_ticks_per_pp,
                        "time_value": price - intrinsic,
                        "forward": node.forward,
                        "discount": node.discount,
                        "pair_iv": pair_iv,
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
                    "parity_edge_ticks": None,
                    "parity_size": None,
                    "vega_ticks_per_pp": None,
                    "time_value": None,
                    "forward": node.forward,
                    "discount": node.discount,
                    "pair_iv": pair_mid_iv(node, nodes_by_key),
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


def is_p1_local(row: Dict[str, Any], args: Any) -> bool:
    return bool(
        row.get("basis") == "LOCAL"
        and row.get("parity_edge_ticks") is not None
        and row["parity_edge_ticks"] >= args.min_parity_edge_ticks
        and row.get("parity_size") is not None
        and row["parity_size"] >= args.min_size
        and row.get("vega_ticks_per_pp") is not None
        and row["vega_ticks_per_pp"] >= args.min_vega_ticks_per_pp
    )


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
            "venue", "instrument", "signal", "basis", "1tick", "price", "size",
            "quote_iv%", "ref_iv%", "iv_edge_pp", "net_ticks", "parity_ticks",
            "pair_size", "vega_t/pp", "D", "mark", "score",
        ),
        [
            (
                row["venue"], row["instrument"], row["signal"], row["basis"],
                row.get("one_tick", "-"), row["price"], row["size"],
                row["quote_iv"] * 100, row["local_iv"] * 100, row["iv_edge_pp"],
                row["net_edge_ticks"], row.get("parity_edge_ticks"),
                row.get("parity_size"), row.get("vega_ticks_per_pp"),
                row.get("discount"), row["mark"], row["score"],
            )
            for row in shown
        ],
    )
