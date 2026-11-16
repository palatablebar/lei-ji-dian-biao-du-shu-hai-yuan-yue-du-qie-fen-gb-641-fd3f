"""表计契约：容量、综合倍率、寄存器组合、单位、换表事件。

契约是全部计算的前提：没有契约里的满量与倍率，读数只是数字。
契约本身也要校验——重复表号、零倍率、未知单位都会让下游算错。
"""
from __future__ import annotations

import csv
import datetime as dt
import io
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from ... import config, errors, reasons
from . import units

FIELDS = ("meter_id", "name", "capacity", "multiplier", "unit", "registers",
          "replaced_at", "replaced_by", "install_reading", "install_order")


@dataclass(frozen=True)
class MeterContract:
    meter_id: str
    name: str
    capacity: Decimal
    multiplier: Decimal
    unit: str            # 契约单位（kWh / MWh），读数先归一到它
    registers: tuple[str, ...]
    replaced_at: dt.datetime | None
    replaced_by: str | None
    tariff: str
    install_reading: Decimal | None   # 装表底码：新表挂网时的起始示数
    install_order: str | None         # 装工单号：底码的证据，缺了底码也不能采信

    @property
    def primary_register(self) -> str:
        """主寄存器：总表优先，没有总表就取第一个分量。"""
        return "TOTAL" if "TOTAL" in self.registers else self.registers[0]

    @property
    def rollover_floor(self) -> Decimal:
        return (self.capacity * config.ROLLOVER_MIN_RATIO).quantize(Decimal("0.01"))

    @property
    def install_proven(self) -> bool:
        """装表证据充分 = 底码与装工单同时存在；缺一样都不能凭首读猜电量。"""
        return self.install_reading is not None and bool(self.install_order)


def _fail(message: str, meter_id: str = "") -> None:
    raise errors.ContractInvalid(message, code="E_CONTRACT_INVALID",
                                 detail={"meter_id": meter_id})


def _parse_instant(raw: str) -> dt.datetime | None:
    text = (raw or "").strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00").replace("/", "-")
    try:
        value = dt.datetime.fromisoformat(text)
    except ValueError:
        _fail(f"换表时点无法解析：{raw!r}")
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=config.CHINA_TZ)
    return value.astimezone(config.UTC)


def _parse_install_reading(raw: str, meter_id: str,
                           capacity: Decimal) -> Decimal | None:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        value = Decimal(text)
    except Exception:
        _fail(f"装表底码不是数字：{meter_id}", meter_id)
        return None
    if value < 0:
        _fail(f"装表底码不能为负：{meter_id}", meter_id)
    if value > capacity:
        _fail(f"装表底码 {value} 超过满量 {capacity}：{meter_id}", meter_id)
    return value


def parse_rows(rows: list[dict]) -> dict[str, MeterContract]:
    """meter_id 保持字符串：`007` 不能变成 `7`。"""
    contracts: dict[str, MeterContract] = {}
    for row in rows:
        if None in row or None in row.values():
            # DictReader 对列数不符静默错位：多列进 None 键、缺列补 None 值
            _fail(f"契约行列数与表头不一致：{row.get('meter_id')!r}")
        meter_id = (row.get("meter_id") or "").strip()
        if not meter_id:
            _fail("表计契约存在空表号")
        if meter_id in contracts:
            _fail(f"表号重复：{meter_id}", meter_id)
        try:
            capacity = Decimal((row.get("capacity") or "").strip())
            multiplier = Decimal((row.get("multiplier") or "").strip())
        except Exception:
            _fail(f"容量或倍率不是数字：{meter_id}", meter_id)
        if capacity <= 0:
            _fail(f"容量必须大于 0：{meter_id}", meter_id)
        if multiplier <= 0:
            _fail(f"倍率必须大于 0：{meter_id}", meter_id)
        unit = units.canonical_unit((row.get("unit") or "").strip())
        if unit is None:
            _fail(f"契约单位无法识别：{meter_id}", meter_id)
        registers = tuple(
            item.strip().upper() for item in (row.get("registers") or "").split(";")
            if item.strip())
        if not registers:
            _fail(f"契约缺少寄存器：{meter_id}", meter_id)
        unknown = [r for r in registers if r not in config.REGISTERS]
        if unknown:
            _fail(f"契约寄存器未知：{meter_id} {'/'.join(unknown)}", meter_id)
        replaced_by = (row.get("replaced_by") or "").strip() or None
        contracts[meter_id] = MeterContract(
            meter_id=meter_id,
            name=(row.get("name") or "").strip() or meter_id,
            capacity=capacity,
            multiplier=multiplier,
            unit=unit,
            registers=registers,
            replaced_at=_parse_instant(row.get("replaced_at") or ""),
            replaced_by=replaced_by,
            tariff=(row.get("tariff") or "单一制").strip(),
            install_reading=_parse_install_reading(
                row.get("install_reading") or "", meter_id, capacity),
            install_order=(row.get("install_order") or "").strip() or None,
        )
    _validate_swap_links(contracts)
    return contracts


def _validate_swap_links(contracts: dict[str, MeterContract]) -> None:
    """换表链完整性：目标存在、时点成对、一表一继、不成环。

    链式换表（A→B→C）是合法的；环与多表同继会让「首读起算」追错对象，
    必须在契约阶段整批失败，而不是在还原时静默丢段。
    """
    successors: dict[str, str] = {}
    for contract in contracts.values():
        if not contract.replaced_by:
            if contract.replaced_at is not None:
                _fail(f"换表时点与新表号必须成对出现：{contract.meter_id}",
                      contract.meter_id)
            continue
        if contract.replaced_by not in contracts:
            _fail(f"换表目标不存在：{contract.replaced_by}", contract.meter_id)
        if contract.replaced_at is None:
            _fail(f"换表缺少时点：{contract.meter_id}", contract.meter_id)
        if contract.replaced_by in successors:
            _fail(f"多块旧表指向同一新表：{contract.replaced_by}",
                  contract.replaced_by)
        successors[contract.replaced_by] = contract.meter_id
    for start in contracts:
        seen: set[str] = set()
        cursor = start
        while cursor in contracts and contracts[cursor].replaced_by:
            nxt = contracts[cursor].replaced_by
            if nxt in seen or nxt == start:
                _fail(f"换表链成环：{start}", start)
            seen.add(cursor)
            cursor = nxt


def load(settings) -> dict[str, MeterContract]:
    path = Path(settings.sample_dir) / config.METERS_FILE
    if not path.is_file():
        _fail(f"表计契约文件缺失：{config.METERS_FILE}")
    text = path.read_text(encoding="utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(text)))
    return parse_rows(rows)


def load_prices(settings) -> dict[str, Decimal]:
    path = Path(settings.sample_dir) / config.PRICES_FILE
    if not path.is_file():
        return dict(config.DEFAULT_PRICES)
    text = path.read_text(encoding="utf-8-sig")
    prices: dict[str, Decimal] = {}
    for row in csv.DictReader(io.StringIO(text)):
        key = (row.get("register") or "").strip().upper()
        if key in config.REGISTERS:
            prices[key] = Decimal((row.get("price") or "0").strip())
    return prices or dict(config.DEFAULT_PRICES)


def payload(contracts: dict[str, MeterContract]) -> list[dict]:
    rows = []
    for contract in sorted(contracts.values(), key=lambda c: c.meter_id):
        rows.append({
            "meter_id": contract.meter_id,
            "name": contract.name,
            "capacity": str(contract.capacity),
            "multiplier": str(contract.multiplier),
            "unit": contract.unit,
            "registers": [config.REGISTER_LABELS.get(r, r) for r in contract.registers],
            "primary_register": config.REGISTER_LABELS.get(contract.primary_register,
                                                           contract.primary_register),
            "replaced_at": (contract.replaced_at.isoformat()
                            if contract.replaced_at else None),
            "replaced_by": contract.replaced_by,
            "tariff": contract.tariff,
            "install_reading": (str(contract.install_reading)
                                if contract.install_reading is not None else None),
            "install_order": contract.install_order,
            "install_proven": contract.install_proven,
            "rollover_floor": str(contract.rollover_floor),
        })
    return rows


def quarantine_row(row: dict, code: str, detail: str) -> dict:
    return {"lineno": 0, "segment": 0, "code": code, "stage": "contract",
            "level": reasons.level(code), "field": "contract",
            "raw_value": row.get("meter_id"), "meter_id": row.get("meter_id"),
            "detail": detail}
