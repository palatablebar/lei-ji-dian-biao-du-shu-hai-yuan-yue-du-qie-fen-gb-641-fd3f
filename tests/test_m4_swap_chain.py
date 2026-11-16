"""链式换表：多次换表接成整条链，无法证明的缺口单独标明、不计电量。

规则：
- 新表首段只认「装表底码 + 装工单」；证据不足记 K_INSTALL_MISSING 缺口，
  首读仍作后续区间锚点，不凭首读猜用电量；
- 首读低于装表底码记 K_INSTALL_MISMATCH 缺口，端点保留双方原值；
- 旧表末读缺失记 K_FINAL_MISSING 缺口；
- 缺口电量恒 0，区间 / 月度 / 账单 / 导出四处口径一致。
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.modules.m6_batches import service
from fixtures import expected

SAMPLE = "readings_swap_chain.csv"


@pytest.fixture()
def chain_result(settings, store) -> dict:
    return service.run(settings, store, sample=SAMPLE)


@pytest.fixture()
def chain_id(chain_result) -> str:
    return chain_result["batch"]["batch_id"]


def _series(store, batch_id, meter_id) -> list[dict]:
    rows = [item for item in store.load_intervals(batch_id)
            if item["meter_id"] == meter_id]
    return sorted(rows, key=lambda item: item["start_at"])


def test_chain_links_all_three_generations(store, chain_id):
    """M0010→M0011→M0012 三段链全部还原，中间表首末都接续。"""
    first = _series(store, chain_id, "M0010")
    second = _series(store, chain_id, "M0011")
    third = _series(store, chain_id, "M0012")
    assert [item["method"] for item in first] == ["normal", "gap_unproven"]
    assert [item["method"] for item in second] == [
        "gap_unproven", "normal", "normal"]
    assert [item["method"] for item in third] == ["replacement_first", "normal"]
    # 链上相邻段的边界时刻一致：旧表末段止 = 换表时点 = 新表首段起
    assert first[-1]["end_at"] == second[0]["start_at"]
    assert second[-1]["end_at"] == third[0]["start_at"]


def test_final_reading_missing_marks_gap(store, chain_id):
    """一代末读停在 01-20，换表时点 02-01：末段缺口单独标明，不计电量。"""
    gap = _series(store, chain_id, "M0010")[-1]
    assert gap["method"] == "gap_unproven"
    assert gap["usage_kwh"] == Decimal("0.000")
    assert "K_GAP_UNPROVEN" in gap["flags"]
    assert "K_FINAL_MISSING" in gap["flags"]
    assert gap["prev_reading"] == Decimal("860.0")  # 末读原值保留
    assert gap["end_at"] == "2026-01-31T16:00:00+00:00"  # 换表时点 02-01 +08:00


def test_unproven_install_not_counted_from_zero(store, chain_id):
    """二代无装表底码：首段不按零计入，首读 45 仍作后续区间锚点。"""
    series = _series(store, chain_id, "M0011")
    gap = series[0]
    assert gap["method"] == "gap_unproven"
    assert gap["usage_kwh"] == Decimal("0.000")
    assert "K_INSTALL_MISSING" in gap["flags"]
    follow = series[1]
    assert follow["prev_reading"] == Decimal("45.0")  # 首读没有丢，继续作锚点
    assert follow["method"] == "normal"
    total = sum((item["usage_kwh"] for item in series), Decimal("0"))
    assert total == expected.CHAIN_M0011_USAGE


def test_proven_install_base_starts_from_base(store, chain_id):
    """三代底码 100 + 装工单齐全：首段从底码起算，不是从零。"""
    first = _series(store, chain_id, "M0012")[0]
    assert first["method"] == "replacement_first"
    assert first["prev_reading"] == Decimal("100")
    assert first["cur_reading"] == Decimal("135.0")
    assert first["usage_kwh"] == expected.CHAIN_M0012_FIRST
    assert "K_INSTALL_BASE" in first["flags"]


def test_first_reading_below_install_base_marks_mismatch(store, chain_id):
    """首读 480 < 底码 500：证据对不上，缺口保留双方原值，首读仍作锚点。"""
    series = _series(store, chain_id, "M0014")
    gap = series[0]
    assert gap["method"] == "gap_unproven"
    assert "K_INSTALL_MISMATCH" in gap["flags"]
    assert gap["prev_reading"] == Decimal("500")   # 装表底码
    assert gap["cur_reading"] == Decimal("480.0")  # 首读原值
    assert gap["usage_kwh"] == Decimal("0.000")
    follow = series[1]
    assert follow["prev_reading"] == Decimal("480.0")
    assert follow["usage_kwh"] == expected.CHAIN_M0014_USAGE


def test_gap_segments_carry_zero_usage_everywhere(store, chain_id):
    """区间 / 月度 / 账单一致：缺口电量恒 0，合计只含可证明用量。"""
    totals = store.load_totals(chain_id)
    assert totals["gap_count"] == str(expected.CHAIN_GAPS)
    assert Decimal(totals["interval_usage"]) == expected.CHAIN_USAGE
    assert Decimal(totals["month_usage"]) == expected.CHAIN_USAGE
    assert Decimal(totals["bill_amount"]) == expected.CHAIN_AMOUNT
    gaps = [item for item in store.load_intervals(chain_id)
            if item["method"] == "gap_unproven"]
    assert len(gaps) == expected.CHAIN_GAPS
    assert all(item["usage_kwh"] == Decimal("0.000") for item in gaps)
    # 月度分摊不含缺口行：分摊行数 = 计量区间聚合后的行数
    allocations = store.load_allocations(chain_id)
    assert len(allocations) == expected.CHAIN_ALLOCATIONS
    assert all(item["usage_kwh"] > 0 for item in allocations)
    bills = store.load_bills(chain_id)
    assert len(bills) == expected.CHAIN_BILLS
    assert sum((bill["usage_kwh"] for bill in bills), Decimal("0")) == \
        expected.CHAIN_USAGE


def test_chain_month_totals(store, chain_id):
    monthly: dict[str, Decimal] = {}
    for bill in store.load_bills(chain_id):
        monthly[bill["month"]] = monthly.get(bill["month"], Decimal("0")) \
            + bill["usage_kwh"]
    assert monthly == expected.CHAIN_MONTH_TOTALS


def test_original_readings_kept_for_review(store, chain_id):
    """复核明细不丢原始读数：11 行全部有效落库，读数账平衡。"""
    readings = store.load_readings(chain_id)
    assert len(readings) == expected.CHAIN_RAW
    assert all(item["status"] == "valid" for item in readings)
    assert {item["reading_text"] for item in readings} >= {"45.0", "480.0", "135.0"}
    totals = store.load_totals(chain_id)
    assert int(totals["raw_lines"]) == expected.CHAIN_RAW
    assert int(totals["valid"]) == expected.CHAIN_RAW
    assert int(totals["quarantine"]) == 0


def test_chain_run_is_balanced(chain_result):
    assert chain_result["reconciliation"]["all_balanced"] is True
    counts = chain_result["counts"]
    assert counts["intervals"] == expected.CHAIN_INTERVALS
    assert counts["readings"] == expected.CHAIN_RAW


def test_rerun_adds_no_new_usage(settings, store, chain_result, chain_id):
    """同一批重跑：行数、合计、批次号逐项不变，不新增一度电。"""
    before_rows = store.count_rows(chain_id)
    before_totals = store.load_totals(chain_id)
    again = service.run(settings, store, sample=SAMPLE)
    assert again["idempotent_replay"] is True
    assert again["batch"]["batch_id"] == chain_id
    assert store.count_rows(chain_id) == before_rows
    assert store.load_totals(chain_id) == before_totals
    assert Decimal(again["totals"]["interval_usage"]) == expected.CHAIN_USAGE


def test_forced_rerun_replaces_in_place(settings, store, chain_result, chain_id):
    """强制重跑同一批：同事务先删后插，行数不翻倍。"""
    again = service.run(settings, store, sample=SAMPLE, mode="force")
    assert again["status"] == "done"
    assert again["batch"]["batch_id"] == chain_id
    assert store.count_rows(chain_id)["intervals"] == expected.CHAIN_INTERVALS
    assert store.count_rows(chain_id)["readings"] == expected.CHAIN_RAW


def test_failed_run_recovers_after_contract_fix(settings, tmp_path):
    """契约成环 → 整批失败不落半批；修复后重跑成功，失败批次保持原样。"""
    import shutil
    from app import errors
    from app.store import Store

    sample_dir = tmp_path / "samples"
    shutil.copytree(settings.sample_dir, sample_dir)
    from app import config as config_mod
    broken_settings = config_mod.Settings(
        db_path=str(tmp_path / "broken.sqlite"), sample_dir=str(sample_dir))
    store = Store(broken_settings.db_path)

    meters = sample_dir / "meters.csv"
    original = meters.read_text(encoding="utf-8")
    # 造环：M0012 → M0010，链 A→B→C→A
    meters.write_text(original.replace(
        "M0012,链式泵站三代,99999.9,1,kWh,TOTAL,,,100,ZB-2026-0301",
        "M0012,链式泵站三代,99999.9,1,kWh,TOTAL,2026-04-01T00:00:00+08:00,M0010,100,ZB-2026-0301"),
        encoding="utf-8")
    with pytest.raises(errors.MeterbackError) as excinfo:
        service.run(broken_settings, store, sample=SAMPLE)
    assert excinfo.value.code == "E_CONTRACT_INVALID"
    failed = store.latest_batch(status="failed")
    assert failed is not None
    assert store.count_rows(failed["batch_id"]) == {
        "meters": 0, "readings": 0, "intervals": 0,
        "allocations": 0, "bills": 0, "quarantine": 0}

    # 修复契约后重跑：新批次成功，失败批次仍是 0 行，可审计
    meters.write_text(original, encoding="utf-8")
    recovered = service.run(broken_settings, store, sample=SAMPLE)
    assert recovered["status"] == "done"
    assert recovered["batch"]["batch_id"] != failed["batch_id"]
    assert store.count_rows(failed["batch_id"])["intervals"] == 0
    assert Decimal(recovered["totals"]["interval_usage"]) == expected.CHAIN_USAGE


# ---------- 单元级：直接构造契约与读数，覆盖链上边界 ----------

def _contracts():
    from app.modules.m2_contract import contract
    return contract.parse_rows([
        {"meter_id": "A", "name": "一代", "capacity": "99999.9", "multiplier": "1",
         "unit": "kWh", "registers": "TOTAL",
         "replaced_at": "2026-02-01T00:00:00+08:00", "replaced_by": "B",
         "install_reading": "", "install_order": ""},
        {"meter_id": "B", "name": "二代", "capacity": "99999.9", "multiplier": "1",
         "unit": "kWh", "registers": "TOTAL",
         "replaced_at": "2026-03-01T00:00:00+08:00", "replaced_by": "C",
         "install_reading": "", "install_order": ""},
        {"meter_id": "C", "name": "三代", "capacity": "99999.9", "multiplier": "1",
         "unit": "kWh", "registers": "TOTAL", "replaced_at": "", "replaced_by": "",
         "install_reading": "200", "install_order": "ZB-9"},
    ])


def _reading(meter: str, read_at: str, value: str, lineno: int = 1) -> dict:
    return {"lineno": lineno, "segment": 1, "meter_id": meter, "register": "TOTAL",
            "read_at": read_at, "reading": Decimal(value), "read_type": "N",
            "flags": []}


def _build(valid):
    from app.modules.m4_intervals import restore
    return restore.build_intervals(valid, [], _contracts())


def test_middle_meter_with_both_gaps():
    """中间表 B：无底码（首段缺口）且末读早于换表时点（末段缺口），链仍不断。"""
    intervals = _build([
        _reading("A", "2026-01-10T16:00:00+00:00", "10"),   # 01-11 +08:00
        _reading("A", "2026-01-31T16:00:00+00:00", "40"),   # 恰在换表时点
        _reading("B", "2026-02-05T16:00:00+00:00", "7"),    # 首读，无底码
        _reading("B", "2026-02-10T16:00:00+00:00", "19"),   # 末读，早于 03-01
        _reading("C", "2026-03-02T16:00:00+00:00", "260"),  # 首读 ≥ 底码 200
    ])
    b_series = [item for item in intervals if item["meter_id"] == "B"]
    assert [item["method"] for item in b_series] == [
        "gap_unproven", "normal", "gap_unproven"]
    assert "K_INSTALL_MISSING" in b_series[0]["flags"]
    assert "K_FINAL_MISSING" in b_series[2]["flags"]
    assert b_series[2]["end_at"] == "2026-02-28T16:00:00+00:00"  # B→C 换表时点
    # 中间表计量只有 19 − 7 = 12
    assert sum((item["usage_kwh"] for item in b_series), Decimal("0")) == \
        Decimal("12.000")
    # 链首尾相接：A 末段止 = B 首段起，B 末段止 = C 首段起
    a_series = [item for item in intervals if item["meter_id"] == "A"]
    c_series = [item for item in intervals if item["meter_id"] == "C"]
    assert a_series[-1]["end_at"] == b_series[0]["start_at"]
    assert b_series[-1]["end_at"] == c_series[0]["start_at"]
    assert c_series[0]["method"] == "replacement_first"
    assert c_series[0]["prev_reading"] == Decimal("200")


def test_first_reading_equal_to_install_base_yields_no_segment():
    """首读 == 底码：首段用量为 0，不生成段，首读直接作锚点。"""
    intervals = _build([
        _reading("A", "2026-01-31T16:00:00+00:00", "5"),
        _reading("C", "2026-03-02T16:00:00+00:00", "200"),  # 首读 == 底码
        _reading("C", "2026-03-05T16:00:00+00:00", "230"),
    ])
    c_series = [item for item in intervals if item["meter_id"] == "C"]
    assert len(c_series) == 1
    assert c_series[0]["method"] == "normal"
    assert c_series[0]["prev_reading"] == Decimal("200")
    assert c_series[0]["usage_kwh"] == Decimal("30.000")


def test_first_reading_at_swap_instant_yields_no_segment():
    """首读恰在换表时点：没有首段也没有缺口，首读直接作锚点。"""
    intervals = _build([
        _reading("A", "2026-01-31T16:00:00+00:00", "5"),
        _reading("C", "2026-02-28T16:00:00+00:00", "210"),  # 恰在 B→C 换表时点
        _reading("C", "2026-03-05T16:00:00+00:00", "240"),
    ])
    c_series = [item for item in intervals if item["meter_id"] == "C"]
    assert len(c_series) == 1
    assert c_series[0]["method"] == "normal"
    assert c_series[0]["usage_kwh"] == Decimal("30.000")


def test_gap_turns_metered_after_evidence_supplied(settings, tmp_path):
    """补录装表底码与装工单后重跑：缺口段转为可计量，总量只增这一段。"""
    import shutil
    from app import config as config_mod
    from app.store import Store

    sample_dir = tmp_path / "samples"
    shutil.copytree(settings.sample_dir, sample_dir)
    local = config_mod.Settings(db_path=str(tmp_path / "fix.sqlite"),
                                sample_dir=str(sample_dir))
    store = Store(local.db_path)

    before = service.run(local, store, sample=SAMPLE)
    assert Decimal(before["totals"]["interval_usage"]) == expected.CHAIN_USAGE

    meters = sample_dir / "meters.csv"
    text = meters.read_text(encoding="utf-8")
    meters.write_text(text.replace(
        "M0011,链式泵站二代,99999.9,1,kWh,TOTAL,2026-03-01T00:00:00+08:00,M0012,,",
        "M0011,链式泵站二代,99999.9,1,kWh,TOTAL,2026-03-01T00:00:00+08:00,M0012,0,ZB-2026-0201"),
        encoding="utf-8")
    after = service.run(local, store, sample=SAMPLE)
    assert after["status"] == "done"
    assert after["batch"]["batch_id"] != before["batch"]["batch_id"]  # 输入变了是新批次
    # M0011 首段从底码 0 起算：45 kWh 转正，其余不变
    assert Decimal(after["totals"]["interval_usage"]) == \
        expected.CHAIN_USAGE + Decimal("45.000")
    assert Decimal(after["totals"]["bill_amount"]) == \
        expected.CHAIN_AMOUNT + Decimal("29.25")
    assert int(after["totals"]["gap_count"]) == expected.CHAIN_GAPS - 1
    assert after["reconciliation"]["all_balanced"] is True


def test_meter_without_readings_yields_no_gap():
    """表没有任何有效读数：无锚点可挂，不产生缺口段。"""
    intervals = _build([
        _reading("A", "2026-01-31T16:00:00+00:00", "5"),
        _reading("C", "2026-03-02T16:00:00+00:00", "260"),
    ])
    assert [item for item in intervals if item["meter_id"] == "B"] == []
