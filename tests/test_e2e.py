"""端到端：脏样例 → 采信 → 隔离 → 还原 → 切分 → 对账 → 导出，全部走 HTTP。"""
from __future__ import annotations

from decimal import Decimal

from fixtures import expected


def test_full_pipeline_over_http(client):
    response = client.post("/api/runs", json={"sample": "readings_dirty.csv"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "done"
    assert payload["reconcile"]["all_balanced"] is True
    assert int(payload["totals"]["valid"]) == expected.VALID
    assert int(payload["totals"]["quarantine"]) == expected.QUARANTINE

    for path in ("/", "/readings", "/intervals", "/months", "/batches", "/export"):
        page = client.get(path)
        assert page.status_code == 200, path
        assert "meterback" in page.text

    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json()["latest_batch"] == payload["batch_id"]

    monthly = client.get("/api/export/monthly.csv")
    assert monthly.status_code == 200
    assert len(monthly.text) > 500
    assert "4499" not in monthly.text.splitlines()[0]


def test_second_run_reuses_batch_and_rows(client):
    first = client.post("/api/runs", json={"sample": "readings_dirty.csv"}).json()
    second = client.post("/api/runs", json={"sample": "readings_dirty.csv"}).json()
    assert second["batch_id"] == first["batch_id"]
    assert second["idempotent_replay"] is True
    assert second["counts"] == first["counts"]
    assert second["totals"]["interval_usage"] == first["totals"]["interval_usage"]
    assert second["totals"]["bill_amount"] == first["totals"]["bill_amount"]


def test_failure_inputs_return_4xx_with_code(client):
    unknown = client.post("/api/runs", json={"sample": "../secret.csv"})
    assert unknown.status_code == 400
    assert unknown.json()["error"] == "E_SAMPLE_UNKNOWN"

    empty = client.post("/api/readings/import", json={"sample": "readings_empty.csv"})
    assert empty.status_code == 422
    assert empty.json()["error"] == "E_EMPTY_FILE"
    assert empty.json()["detail"]["failed_stage"] == "ingest"

    header = client.post("/api/readings/import", json={"sample": "readings_bad_header.csv"})
    assert header.status_code == 422
    assert header.json()["error"] == "E_HEADER_INVALID"


def test_pages_render_without_any_batch(client):
    for path in ("/", "/readings", "/intervals", "/months", "/batches", "/export"):
        page = client.get(path)
        assert page.status_code == 200
        assert "empty-line" in page.text
    assert client.get("/api/quarantine").json()["rows"] == []
    assert client.get("/api/reconcile").json()["accounts"] == []


def test_swap_chain_pipeline_over_http(client):
    """链式换表样例：缺口单独标明，区间/月度/账单/导出一致，重跑不新增电量。"""
    response = client.post("/api/runs", json={"sample": "readings_swap_chain.csv"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "done"
    assert payload["reconcile"]["all_balanced"] is True
    totals = payload["totals"]
    assert Decimal(totals["interval_usage"]) == expected.CHAIN_USAGE
    assert Decimal(totals["month_usage"]) == expected.CHAIN_USAGE
    assert Decimal(totals["bill_amount"]) == expected.CHAIN_AMOUNT
    assert int(totals["gap_count"]) == expected.CHAIN_GAPS

    for path in ("/", "/readings", "/intervals", "/months", "/batches", "/export"):
        page = client.get(path)
        assert page.status_code == 200, path
    intervals_page = client.get("/intervals")
    assert "不可证明缺口" in intervals_page.text
    assert "换表链" in intervals_page.text
    months_page = client.get("/months")
    assert "不可证明缺口" in months_page.text

    # 区间导出：缺口段与计量段都在，判定列与库内口径一致
    body = client.get("/api/export/intervals.csv").text
    assert body.count("不可证明缺口") == expected.CHAIN_GAPS
    assert "新表首段" in body
    # 读数证据导出：11 行原始读数一行不丢
    readings = client.get("/api/export/readings.csv").text
    assert len(readings.splitlines()) == expected.CHAIN_RAW + 1
    assert "480.0" in readings and "135.0" in readings
    # 清单：缺口计数与四账可离线复核
    manifest = client.get("/api/export/manifest.json").json()
    assert manifest["manifest"]["counters"]["gaps"] == expected.CHAIN_GAPS
    assert manifest["manifest"]["all_balanced"] is True

    # 同一批重跑：批次号、行数、合计逐项不变
    again = client.post("/api/runs", json={"sample": "readings_swap_chain.csv"}).json()
    assert again["idempotent_replay"] is True
    assert again["batch_id"] == payload["batch_id"]
    assert again["counts"] == payload["counts"]
    assert again["totals"]["interval_usage"] == totals["interval_usage"]
