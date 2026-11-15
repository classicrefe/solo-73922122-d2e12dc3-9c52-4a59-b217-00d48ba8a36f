"""温度探头两点校正的端到端测试。

覆盖：
  * 阶段闸门（仅 ready 可校正；backfilling/switched 拒绝）
  * 两点线性换算、四舍五入两位小数、pH 不变、修正前后留痕
  * 整单失败：编号缺失、两点相同、修正值超 temp 列范围、编号已校正（含复用 id）
  * 单号幂等：同号同内容回放首次结果、同号异内容冲突
  * 与旧采集程序直接 SQL 交错：行锁保证使用实际当前值，两项一起提交
  * 校正后分析员沿原 /samples 与 /measurements 接口读到的就是校正值
"""
from __future__ import annotations

import threading
import time
from decimal import Decimal

import psycopg

from _helpers import wait_phase


def _ready(client):
    client.post("/migration/start")
    wait_phase(client, "ready")


def _cal_payload(**over):
    payload = {
        "order_no": "CAL-1",
        "raw1": 20.0, "std1": 21.0,
        "raw2": 30.0, "std2": 32.0,
        "sample_ids": [1, 2],
    }
    payload.update(over)
    return payload


# ------------------------------------------------------------ 阶段闸门
def test_calibration_rejected_before_ready(client):
    # init 阶段直接拒绝
    r = client.post("/calibrations", json=_cal_payload())
    assert r.status_code == 409
    assert "ready" in r.json()["detail"]


def test_calibration_rejected_after_switch(client):
    _ready(client)
    assert client.post("/migration/switch").status_code == 200
    r = client.post("/calibrations", json=_cal_payload())
    assert r.status_code == 409
    assert "ready" in r.json()["detail"]


# ------------------------------------------------------------ 正常换算
def test_calibration_linear_conversion_and_rounding(client):
    _ready(client)
    before = {s["id"]: s for s in client.get("/samples").json()}

    # raw1=20->std1=21, raw2=30->std2=32 => a=1.1, b=-1
    r = client.post("/calibrations", json=_cal_payload(order_no="CAL-A",
                                                       sample_ids=[1, 2]))
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["replayed"] is False
    assert Decimal(str(data["order"]["slope"])) == Decimal("1.1")
    assert Decimal(str(data["order"]["intercept"])) == Decimal("-1.0")

    for s in data["samples"]:
        old = before[s["sample_id"]]
        expected = float(
            (Decimal("1.1") * Decimal(str(old["temp"])) - Decimal("1"))
            .quantize(Decimal("0.01"))
        )
        assert s["temp_before"] == old["temp"]
        assert s["temp_after"] == expected
        # pH 原样保留
        assert s["ph_before"] == s["ph_after"] == old["ph"]

    # 分析员沿原接口即可读到校正值
    after = {s["id"]: s for s in client.get("/samples").json()}
    assert after[1]["temp"] == data["samples"][0]["temp_after"]
    assert after[1]["ph"] == before[1]["ph"]
    group = {m["metric"]: m["value"]
             for m in client.get("/measurements/1").json()}
    assert group["temp"] == after[1]["temp"]
    assert group["ph"] == after[1]["ph"]


def test_calibration_round_half_up_to_two_decimals(client, dsn):
    """四舍五入（ROUND_HALF_UP）到两位小数。"""
    _ready(client)
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("UPDATE samples SET temp=20.00 WHERE id=6")
    # raw 0->std 0，raw 10000->std 1234.5 => a=0.12345，b=0
    # 样本6 temp=20 => 20*0.12345 = 2.469 -> 2.47
    r = client.post("/calibrations", json=_cal_payload(
        order_no="CAL-H", raw1=0, std1=0, raw2=10000, std2=1234.5,
        sample_ids=[6]))
    assert r.status_code == 200, r.text
    assert r.json()["samples"][0]["temp_after"] == 2.47


# ------------------------------------------------------------ 整单失败
def test_missing_sample_id_fails_whole_order(client):
    _ready(client)
    before = {s["id"]: s for s in client.get("/samples").json()}
    r = client.post("/calibrations", json=_cal_payload(
        order_no="CAL-MISS", sample_ids=[1, 9999]))
    assert r.status_code == 409
    assert "缺失" in r.json()["detail"]
    # 原值保留：没有任何行被改动，也没有留下校正单
    after = {s["id"]: s for s in client.get("/samples").json()}
    assert before == after
    assert client.get("/calibrations").json() == []


def test_equal_raw_points_rejected(client):
    _ready(client)
    r = client.post("/calibrations", json=_cal_payload(
        order_no="CAL-EQ", raw1=25.0, raw2=25.0))
    assert r.status_code == 409
    assert "相同" in r.json()["detail"]
    assert client.get("/calibrations").json() == []


def test_corrected_value_out_of_temp_range_fails(client):
    _ready(client)
    # 取一个极大斜率让修正值必然超出 NUMERIC(8,2) 上限 999999.99
    r = client.post("/calibrations", json=_cal_payload(
        order_no="CAL-RANGE", raw1=0, std1=0, raw2=1, std2=1000000,
        sample_ids=[1]))
    assert r.status_code == 409
    assert "temp 列范围" in r.json()["detail"]
    # 原值保留
    s = client.get("/samples/1").json()
    assert s["temp"] == 22.50


def test_more_than_20_samples_rejected(client):
    _ready(client)
    r = client.post("/calibrations", json=_cal_payload(
        order_no="CAL-21", sample_ids=list(range(1, 22))))
    assert r.status_code == 422


def test_sample_id_can_only_be_calibrated_once_even_after_reuse(client, dsn):
    _ready(client)
    r = client.post("/calibrations", json=_cal_payload(
        order_no="CAL-ONCE", sample_ids=[7]))
    assert r.status_code == 200
    corrected_7 = client.get("/samples/7").json()["temp"]

    # 旧采集程序删除后复用同一编号
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DELETE FROM samples WHERE id=7")
        conn.execute(
            "INSERT INTO samples (id, temp, ph) OVERRIDING SYSTEM VALUE "
            "VALUES (7, 12.34, 6.50)"
        )

    # 新单包含被复用的 7 号：整单失败，新行原值保留
    r = client.post("/calibrations", json=_cal_payload(
        order_no="CAL-REUSE", sample_ids=[7, 8]))
    assert r.status_code == 409
    assert "不能再次校正" in r.json()["detail"]
    assert client.get("/samples/7").json()["temp"] == 12.34
    assert client.get("/samples/8").json()["temp"] == 21.30
    # 只有第一张单留痕
    orders = [o["order_no"] for o in client.get("/calibrations").json()]
    assert orders == ["CAL-ONCE"]
    # 历史详情仍可回放首次校正结果
    detail = client.get("/calibrations/CAL-ONCE").json()
    assert detail["samples"][0]["sample_id"] == 7
    assert detail["samples"][0]["temp_after"] == corrected_7


# ------------------------------------------------------------ 单号幂等
def test_same_order_same_content_replays_first_result(client):
    _ready(client)
    payload = _cal_payload(order_no="CAL-IDEM", sample_ids=[9, 10])
    first = client.post("/calibrations", json=payload)
    assert first.status_code == 200
    first_data = first.json()
    assert first_data["replayed"] is False

    second = client.post("/calibrations", json=payload)
    assert second.status_code == 200
    second_data = second.json()
    assert second_data["replayed"] is True
    # 回放的是首次结果，而非按新当前值重算
    assert second_data["samples"] == first_data["samples"]


def test_same_order_different_content_conflicts(client):
    _ready(client)
    r1 = client.post("/calibrations", json=_cal_payload(
        order_no="CAL-CONF", sample_ids=[11]))
    assert r1.status_code == 200
    r2 = client.post("/calibrations", json=_cal_payload(
        order_no="CAL-CONF", std2=33.0, sample_ids=[11]))
    assert r2.status_code == 409
    assert "冲突" in r2.json()["detail"]
    # 数据保持首次校正的结果
    assert client.get("/samples/11").json()["temp"] == \
        r1.json()["samples"][0]["temp_after"]


# ------------------------------------------------------------ 与直接 SQL 交错
def test_concurrent_direct_sql_uses_actually_modified_current_value(client, dsn):
    _ready(client)
    barrier = threading.Event()

    def legacy_update():
        # 等待校正事务拿到行锁后，用直接 SQL 改当前值（应阻塞到校正提交）
        barrier.wait(timeout=10)
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("UPDATE samples SET temp=25.00, ph=7.80 WHERE id=2")

    t = threading.Thread(target=legacy_update)
    t.start()

    # 用独立池连接手动驱动：先在事务内 FOR UPDATE 取锁并放行旧程序更新，
    # 再在同一事务里提交校正单，验证换算使用的是加锁时刻的实际当前值
    from app import db as dbmod
    from app.calibration import submit_calibration
    from app.migration import get_phase

    with dbmod.pool.connection() as conn:
        assert get_phase(conn) == "ready"
        cur = conn.cursor()
        cur.execute("SELECT id, temp, ph FROM samples WHERE id = ANY(%s) "
                    "ORDER BY id FOR UPDATE", ([1, 2],))
        locked = cur.fetchall()
        assert [r[0] for r in locked] == [1, 2]
        original_temp_2 = locked[1][1]
        barrier.set()  # 旧程序 UPDATE 开始等待行锁
        time.sleep(0.5)

        result = submit_calibration(
            conn, order_no="CAL-CONC",
            raw1=Decimal("20"), std1=Decimal("21"),
            raw2=Decimal("30"), std2=Decimal("32"),
            sample_ids=[1, 2],
        )

    t.join(timeout=10)

    # 校正使用的是加锁时刻的实际当前值（25.00 的更新在提交后才生效）
    s2 = next(s for s in result["samples"] if s["sample_id"] == 2)
    expected = float((Decimal("1.1") * Decimal(str(original_temp_2))
                      - Decimal("1")).quantize(Decimal("0.01")))
    assert s2["temp_before"] == float(original_temp_2)
    assert s2["temp_after"] == expected

    # 旧程序的更新在校正提交后落地：两项（temp+ph）一起成功
    row = client.get("/samples/2").json()
    assert row["temp"] == 25.00 and row["ph"] == 7.80
    group = {m["metric"]: m["value"]
             for m in client.get("/measurements/2").json()}
    assert group == {"temp": 25.00, "ph": 7.80}


def test_calibration_updates_both_metrics_together(client, dsn):
    """校正 UPDATE 经触发器同时维护长表两项，不存在半组数据。"""
    _ready(client)
    r = client.post("/calibrations", json=_cal_payload(
        order_no="CAL-ATOM", sample_ids=[12]))
    assert r.status_code == 200
    with psycopg.connect(dsn) as conn:
        n = conn.execute(
            "SELECT count(*) FROM measurements WHERE sample_id=12"
        ).fetchone()[0]
        assert n == 2
        vals = dict(conn.execute(
            "SELECT metric, value FROM measurements WHERE sample_id=12"
        ).fetchall())
    s = client.get("/samples/12").json()
    assert float(vals["temp"]) == s["temp"]
    assert float(vals["ph"]) == s["ph"]
