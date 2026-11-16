"""区间还原：按 (表号, 寄存器, UTC 时间) 排序后逐对还原用量。

三件事一起做，缺一条结果就错：
1. 满量翻转补容量，倒走不补（倒走已在质量层隔离）；
2. 换表拆两段：旧表到换表时点为止，新表首段从装表底码起算；
3. 估抄行不作锚点，但它落在区间内时给区间打标记。

换表首段只认「装表底码 + 装工单」这对证据：证据不足或首读与底码对不上时，
不能把首读按零计入——那段用量记 `gap_unproven` 缺口，电量恒 0，
首读本身仍作后续区间的锚点，表链不断、原始读数不丢。
旧表末读缺失（末读时刻早于换表时点）同样记缺口，不悄悄吞掉。
"""
from __future__ import annotations

from decimal import Decimal

from ... import config
from ..m2_contract import coerce, contract as contract_mod, units
from . import rollover, swaps

DAYS_QUANT = Decimal("0.000000001")

GAP_METHOD = "gap_unproven"


def _days(start: str, end: str) -> Decimal:
    import datetime as dt
    left = dt.datetime.fromisoformat(start)
    right = dt.datetime.fromisoformat(end)
    return (Decimal((right - left).total_seconds()) / Decimal(86400)).quantize(DAYS_QUANT)


def _markers(method: str) -> list[str]:
    if method == "rollover":
        return ["K_ROLLOVER"]
    if method == "replacement_first":
        return ["K_INSTALL_BASE"]
    return []


def build_intervals(valid: list[dict], estimates: list[dict],
                    contracts: dict[str, contract_mod.MeterContract]) -> list[dict]:
    estimate_index: dict[tuple, list[str]] = {}
    for item in estimates:
        estimate_index.setdefault((item["meter_id"], item["register"]), []).append(
            item["read_at"])
    predecessors = swaps.predecessor_map(contracts)

    series: dict[tuple, list[dict]] = {}
    for record in valid:
        series.setdefault((record["meter_id"], record["register"]), []).append(record)

    intervals: list[dict] = []
    for key in sorted(series):
        meter_id, register = key
        meter = contracts[meter_id]
        predecessor = predecessors.get(meter_id)
        items = sorted(series[key], key=lambda item: item["read_at"])
        seq = 0
        last: dict | None = None
        for item in items:
            if last is None:
                last = item
                if predecessor is not None and predecessor.replaced_at is not None:
                    start = predecessor.replaced_at.isoformat()
                    if item["read_at"] > start:
                        first = _swap_first_segment(
                            seq=seq + 1, meter=meter, register=register, start=start,
                            first=item, estimates=estimate_index)
                        if first is not None:
                            seq += 1
                            intervals.append(first)
                continue
            delta, method = rollover.delta_for(last["reading"], item["reading"],
                                               meter.capacity)
            seq += 1
            intervals.append(_make(
                seq=seq, meter=meter, register=register, start=last["read_at"],
                end=item["read_at"], prev=last["reading"], cur=item["reading"],
                method=method, estimates=estimate_index, delta=delta))
            last = item
        final_gap = _swap_final_gap(
            seq=seq + 1, meter=meter, register=register, last=last,
            estimates=estimate_index)
        if final_gap is not None:
            seq += 1
            intervals.append(final_gap)
    intervals.sort(key=lambda item: (item["meter_id"], item["register"], item["start_at"]))
    return intervals


def _swap_first_segment(*, seq: int, meter: contract_mod.MeterContract, register: str,
                        start: str, first: dict,
                        estimates: dict[tuple, list[str]]) -> dict | None:
    """新表首段 [换表时点, 首读]：证据充分才计量，否则只标缺口不猜电量。

    - 底码与装工单齐全且首读 ≥ 底码：从底码起算（首读 == 底码时用量为 0，不生成段）；
    - 首读 < 底码：证据自相矛盾，缺口标 `K_INSTALL_MISMATCH`，端点保留双方原值；
    - 无底码或无装工单：缺口标 `K_INSTALL_MISSING`，不凭首读猜用电量。
    """
    base = meter.install_reading
    if meter.install_proven and first["reading"] >= base:
        delta = first["reading"] - base
        if delta == 0:
            return None
        return _make(seq=seq, meter=meter, register=register, start=start,
                     end=first["read_at"], prev=base, cur=first["reading"],
                     method="replacement_first", estimates=estimates, delta=delta)
    if meter.install_proven:
        return _make(seq=seq, meter=meter, register=register, start=start,
                     end=first["read_at"], prev=base, cur=first["reading"],
                     method=GAP_METHOD, estimates=estimates, delta=Decimal("0"),
                     flags=["K_GAP_UNPROVEN", "K_INSTALL_MISMATCH"])
    return _make(seq=seq, meter=meter, register=register, start=start,
                 end=first["read_at"], prev=first["reading"], cur=first["reading"],
                 method=GAP_METHOD, estimates=estimates, delta=Decimal("0"),
                 flags=["K_GAP_UNPROVEN", "K_INSTALL_MISSING"])


def _swap_final_gap(*, seq: int, meter: contract_mod.MeterContract, register: str,
                    last: dict | None,
                    estimates: dict[tuple, list[str]]) -> dict | None:
    """旧表末段缺口 [末读时刻, 换表时点]：末读缺失时这段用量无法证明。

    末读恰在换表时点（或更晚，已被质量层隔离）则没有缺口。
    """
    if meter.replaced_at is None or last is None:
        return None
    end = meter.replaced_at.isoformat()
    if last["read_at"] >= end:
        return None
    return _make(seq=seq, meter=meter, register=register, start=last["read_at"],
                 end=end, prev=last["reading"], cur=last["reading"],
                 method=GAP_METHOD, estimates=estimates, delta=Decimal("0"),
                 flags=["K_GAP_UNPROVEN", "K_FINAL_MISSING"])


def _make(*, seq: int, meter: contract_mod.MeterContract, register: str, start: str,
          end: str, prev: Decimal, cur: Decimal, method: str,
          estimates: dict[tuple, list[str]], delta: Decimal | None = None,
          flags: list[str] | None = None) -> dict:
    if delta is None:
        delta = cur - prev
    usage_contract = delta * meter.multiplier
    usage_kwh = units.usage_to_kwh(usage_contract, meter.unit).quantize(
        config.USAGE_QUANT)
    marks = list(flags) if flags is not None else _markers(method)
    if method != GAP_METHOD:
        for stamp in estimates.get((meter.meter_id, register), []):
            if start < stamp <= end:
                marks.append("K_ESTIMATE_SKIPPED")
                break
    return {
        "interval_id": f"{meter.meter_id}:{register}:{seq:02d}",
        "meter_id": meter.meter_id,
        "register": register,
        "start_at": start,
        "end_at": end,
        "prev_reading": prev,
        "cur_reading": cur,
        "delta": delta,
        "multiplier": meter.multiplier,
        "unit": meter.unit,
        "usage_kwh": usage_kwh,
        "days": _days(start, end),
        "method": method,
        "flags": marks,
    }


def gaps(intervals: list[dict]) -> list[dict]:
    """不可证明缺口：区间流里 method = gap_unproven 的段，电量恒 0。"""
    return [item for item in intervals if item["method"] == GAP_METHOD]


def totals(intervals: list[dict],
           contracts: dict[str, contract_mod.MeterContract]) -> dict:
    """主寄存器区间用量合计：总表优先，避免与峰平谷重复计算。"""
    primary = {m.meter_id: m.primary_register for m in contracts.values()}
    total = Decimal("0")
    for item in intervals:
        if primary.get(item["meter_id"]) == item["register"]:
            total += item["usage_kwh"]
    return {"primary_usage_kwh": total.quantize(config.USAGE_QUANT),
            "interval_count": len(intervals),
            "gap_count": len(gaps(intervals))}


def primary_intervals(intervals: list[dict],
                      contracts: dict[str, contract_mod.MeterContract]) -> list[dict]:
    primary = {m.meter_id: m.primary_register for m in contracts.values()}
    return [item for item in intervals
            if primary.get(item["meter_id"]) == item["register"]]


__all__ = ["build_intervals", "totals", "primary_intervals", "gaps", "coerce",
           "GAP_METHOD"]
