r"""温度探头两点校正（仅在回填完成、最终切换之前操作）。

业务规则（整单事务，任何一条不满足都整体回滚、原值保留）：

  * 阶段闸门：只允许 migration_state.state = 'ready'（回填完成且未切换）；
  * 两个原始标定点温度不得相同，否则无法确定唯一线性关系；
  * 至多 20 个样本编号，编号在请求内去重；任一编号在 samples 中缺失即整单失败；
  * 换算关系由两点确定：a = (std2-std1)/(raw2-raw1)，b = std1 - a*raw1，
    修正温度 = round(a * 当前温度 + b, 2)（四舍五入到两位小数），pH 保持不变；
  * 修正值必须落在 temp 列 NUMERIC(8,2) 范围内（-999999.99 ~ 999999.99）；
  * 旧采集程序直接 SQL 改/删与校正交错时，事务内先对目标行加行级锁，
    换算一律使用锁保护下“实际被修改的当前值”；经 samples 一个写入面更新，
    双写/视图触发器保证 temp、ph 两项在同一事务一起提交；
  * 校正单号幂等：同号同内容的成功重试回放首次结果；同号不同内容报冲突；
  * 编号一经成功校正即登记（calibration_order_samples.sample_id 唯一），
    之后即使被删除并复用同一编号，也不能再次校正。
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from psycopg.rows import dict_row

from .migration import get_phase

# temp NUMERIC(8,2) 的可表示区间
TEMP_MIN = Decimal("-999999.99")
TEMP_MAX = Decimal("999999.99")
MAX_SAMPLES = 20

TWO_PLACES = Decimal("0.01")


class CalibrationError(Exception):
    """业务校验失败：HTTP 层映射为 409，调用事务整体回滚。"""

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


def _q2(value: Decimal) -> Decimal:
    """四舍五入到两位小数（ROUND_HALF_UP，与 PostgreSQL numeric round 同向）。"""
    return value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def _same_decimal(a: Decimal, b: Decimal) -> bool:
    # 标定点以两位小数语义比较：20 与 20.00 视为同内容
    return _q2(a) == _q2(b)


def submit_calibration(
    conn,
    *,
    order_no: str,
    raw1: Decimal,
    std1: Decimal,
    raw2: Decimal,
    std2: Decimal,
    sample_ids: list[int],
) -> dict:
    order_no = order_no.strip()
    if not order_no:
        raise CalibrationError("校正单号不能为空")

    # 请求内编号去重（保持首次出现顺序），并限制数量
    deduped: list[int] = []
    seen: set[int] = set()
    for sid in sample_ids:
        if sid not in seen:
            seen.add(sid)
            deduped.append(sid)
    if not deduped:
        raise CalibrationError("至少需要一个样本编号")
    if len(deduped) > MAX_SAMPLES:
        raise CalibrationError(f"一次最多校正 {MAX_SAMPLES} 个样本编号")

    if _same_decimal(raw1, raw2):
        raise CalibrationError("两个原始标定点温度相同，无法确定线性关系")

    slope = (std2 - std1) / (raw2 - raw1)
    intercept = std1 - slope * raw1

    with conn.transaction():
        phase = get_phase(conn)
        if phase != "ready":
            raise CalibrationError(
                f"只允许在回填完成且最终切换前（phase=ready）校正，当前 phase={phase}"
            )

        # ---- 单号幂等：已存在的成功单只做回放/冲突判定，不产生任何写入 ----
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT order_no, raw1, std1, raw2, std2, slope, intercept "
                "FROM calibration_orders WHERE order_no = %s",
                (order_no,),
            )
            existing = cur.fetchone()
        if existing is not None:
            same = (
                _same_decimal(existing["raw1"], raw1)
                and _same_decimal(existing["std1"], std1)
                and _same_decimal(existing["raw2"], raw2)
                and _same_decimal(existing["std2"], std2)
            )
            if not same:
                raise CalibrationError(
                    f"校正单号 {order_no} 已使用且标定点内容不同，冲突拒绝"
                )
            return _load_result(conn, order_no, replayed=True)

        # ---- 行级锁锁定全部目标行：与旧采集程序的直接 SQL 串行化 ----
        # FOR UPDATE 让并发 UPDATE/DELETE 等待本事务结束，因此下面读到的
        # 就是实际参与换算的当前值；缺失的编号不会出现在结果集中。
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT id, temp, ph FROM samples WHERE id = ANY(%s) "
                "ORDER BY id FOR UPDATE",
                (deduped,),
            )
            locked = [dict(r) for r in cur.fetchall()]

        if len(locked) != len(deduped):
            found = {r["id"] for r in locked}
            missing = [sid for sid in deduped if sid not in found]
            raise CalibrationError(f"样本编号缺失，整单失败：{missing}")

        # ---- 编号一生只能成功校正一次（含删除后复用 id 的情形）----
        with conn.cursor() as cur:
            cur.execute(
                "SELECT sample_id FROM calibration_order_samples "
                "WHERE sample_id = ANY(%s) ORDER BY sample_id",
                (deduped,),
            )
            already = [r[0] for r in cur.fetchall()]
        if already:
            raise CalibrationError(
                f"编号已被成功校正过，不能再次校正：{already}"
            )

        # ---- 按两点线性关系换算当前温度，先做范围预校验 ----
        details: list[dict] = []
        for row in locked:
            current_temp = Decimal(row["temp"])
            current_ph = Decimal(row["ph"])
            corrected = _q2(slope * current_temp + intercept)
            if corrected < TEMP_MIN or corrected > TEMP_MAX:
                raise CalibrationError(
                    f"样本 {row['id']} 的修正温度 {corrected} 超出 temp 列范围 "
                    f"[{TEMP_MIN}, {TEMP_MAX}]，整单失败"
                )
            details.append(
                {
                    "sample_id": row["id"],
                    "temp_before": current_temp,
                    "temp_after": corrected,
                    "ph_before": current_ph,
                    "ph_after": current_ph,  # pH 保持不变
                }
            )

        # ---- 经 samples 单一写入面更新：表时代走双写触发器、视图时代走
        #      INSTEAD OF 触发器，temp/ph 两项在本事务内一起生效/一起回滚 ----
        with conn.cursor() as cur:
            for d in details:
                cur.execute(
                    "UPDATE samples SET temp = %s, ph = %s WHERE id = %s",
                    (d["temp_after"], d["ph_after"], d["sample_id"]),
                )
                if cur.rowcount != 1:
                    # 锁内不应发生；一旦发生说明行被异常移除，整单回滚保原值
                    raise CalibrationError(
                        f"样本 {d['sample_id']} 更新行数异常，整单回滚"
                    )

        # ---- 落校正台账（order_no 与 sample_id 唯一约束兜底并发）----
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO calibration_orders "
                "(order_no, raw1, std1, raw2, std2, slope, intercept) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (order_no, _q2(raw1), _q2(std1), _q2(raw2), _q2(std2),
                 slope, intercept),
            )
            cur.executemany(
                "INSERT INTO calibration_order_samples "
                "(order_no, sample_id, temp_before, temp_after, ph_before, ph_after) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                [
                    (order_no, d["sample_id"], d["temp_before"], d["temp_after"],
                     d["ph_before"], d["ph_after"])
                    for d in details
                ],
            )

    # 事务提交后再回读，保证返回的是已持久化的首次结果
    return _load_result(conn, order_no, replayed=False)


def _load_result(conn, order_no: str, *, replayed: bool) -> dict:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT order_no, raw1, std1, raw2, std2, slope, intercept, created_at "
            "FROM calibration_orders WHERE order_no = %s",
            (order_no,),
        )
        order = dict(cur.fetchone())
        cur.execute(
            "SELECT sample_id, temp_before, temp_after, ph_before, ph_after "
            "FROM calibration_order_samples WHERE order_no = %s ORDER BY sample_id",
            (order_no,),
        )
        samples = [dict(r) for r in cur.fetchall()]
    return {
        "ok": True,
        "replayed": replayed,
        "order": _jsonable(order),
        "samples": [_jsonable(s) for s in samples],
    }


def list_orders(conn) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT o.order_no, o.raw1, o.std1, o.raw2, o.std2, "
            "       o.slope, o.intercept, o.created_at, "
            "       count(s.sample_id) AS sample_count "
            "FROM calibration_orders o "
            "LEFT JOIN calibration_order_samples s ON s.order_no = o.order_no "
            "GROUP BY o.id, o.order_no, o.raw1, o.std1, o.raw2, o.std2, "
            "         o.slope, o.intercept, o.created_at "
            "ORDER BY o.id"
        )
        return [_jsonable(dict(r)) for r in cur.fetchall()]


def get_order(conn, order_no: str) -> dict | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT order_no, raw1, std1, raw2, std2, slope, intercept, created_at "
            "FROM calibration_orders WHERE order_no = %s",
            (order_no,),
        )
        order = cur.fetchone()
        if order is None:
            return None
        cur.execute(
            "SELECT sample_id, temp_before, temp_after, ph_before, ph_after "
            "FROM calibration_order_samples WHERE order_no = %s ORDER BY sample_id",
            (order_no,),
        )
        samples = [dict(r) for r in cur.fetchall()]
    return {
        "ok": True,
        "replayed": True,
        "order": _jsonable(dict(order)),
        "samples": [_jsonable(s) for s in samples],
    }


def _jsonable(row: dict) -> dict:
    """Decimal/datetime 转成 JSON 友好类型（数值保留两位小数语义）。"""
    out: dict = {}
    for key, value in row.items():
        if isinstance(value, Decimal):
            out[key] = float(value)
        elif hasattr(value, "isoformat"):
            out[key] = value.isoformat()
        else:
            out[key] = value
    return out
