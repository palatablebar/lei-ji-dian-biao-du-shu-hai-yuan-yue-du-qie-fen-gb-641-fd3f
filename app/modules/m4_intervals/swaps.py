"""换表事件与表链：旧表停抄、新表从装表底码起算，换表区间必须拆成两段。

朴素实现把同一块表的上读与下读相减；换表后两个示数来自不同物理表，
相减得到负数，于是整段被丢掉或倒扣。这里把换表点显式建模，
并把多次换表接成整条链：A→B→C 的每个换表点独立判定，
中间表既承接前一段首读，也向后一段交出末读。
"""
from __future__ import annotations

from . import rollover
from ..m2_contract import contract as contract_mod


def replacement_pairs(contracts: dict[str, contract_mod.MeterContract]) -> list[dict]:
    pairs = []
    for contract in contracts.values():
        if contract.replaced_by:
            pairs.append({
                "meter_id": contract.meter_id,
                "replaced_by": contract.replaced_by,
                "replaced_at": contract.replaced_at.isoformat()
                if contract.replaced_at else None,
            })
    return sorted(pairs, key=lambda item: item["meter_id"])


def predecessor_map(contracts: dict[str, contract_mod.MeterContract],
                    ) -> dict[str, contract_mod.MeterContract]:
    """新表 → 旧表契约。"""
    out: dict[str, contract_mod.MeterContract] = {}
    for contract in contracts.values():
        if not contract.replaced_by:
            continue
        if contract.replaced_by not in contracts:
            continue
        out.setdefault(contract.replaced_by, contract)
    return out


def chains(contracts: dict[str, contract_mod.MeterContract]) -> list[list[str]]:
    """整条换表链：从链头（没有前驱的表）沿 replaced_by 走到链尾。

    契约加载已保证不成环、一表一继，这里只管顺序拼接。
    """
    predecessors = predecessor_map(contracts)
    heads = [meter_id for meter_id, contract in contracts.items()
             if contract.replaced_by and meter_id not in predecessors]
    out: list[list[str]] = []
    for head in sorted(heads):
        chain = [head]
        cursor = head
        while contracts[cursor].replaced_by:
            cursor = contracts[cursor].replaced_by
            chain.append(cursor)
        out.append(chain)
    return out


__all__ = ["replacement_pairs", "predecessor_map", "chains", "rollover"]
