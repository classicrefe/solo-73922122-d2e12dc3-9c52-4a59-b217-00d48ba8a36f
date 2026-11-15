-- 04 温度探头两点校正（在 02_expand 之后、仍处于切换前的 samples 表时代由应用引导执行）
-- 全程幂等：应用每次启动在单个事务内执行，已存在的对象不会被重建。
--
-- 设计要点：
--   * calibration_orders 记录每张成功校正单（校正单号、两点参数、换算系数与结果），
--     order_no 唯一：同号同内容重试直接回放首次结果，同号异内容由应用判冲突；
--   * calibration_order_samples 以 sample_id 为主键登记“被成功校正过的编号”，
--     即使该编号之后被旧程序删除并复用，主键约束仍会拒绝再次校正（编号今生只许一次）；
--   * 整单在一个事务内提交：任一编号缺失、已校正过、修正值超 temp 列范围，
--     或两点相同等校验失败时整体回滚，任何原值都不会被改动。

CREATE TABLE IF NOT EXISTS calibration_orders (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    order_no     TEXT        NOT NULL UNIQUE,
    raw1         NUMERIC(10, 2) NOT NULL,
    std1         NUMERIC(10, 2) NOT NULL,
    raw2         NUMERIC(10, 2) NOT NULL,
    std2         NUMERIC(10, 2) NOT NULL,
    slope        NUMERIC(18, 8) NOT NULL,   -- 两点线性关系斜率 a
    intercept    NUMERIC(14, 4) NOT NULL,   -- 截距 b：修正温度 = round(a * 原始 + b, 2)
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 每单的样本明细（修正前后值留痕，供分析员沿原接口复核）
CREATE TABLE IF NOT EXISTS calibration_order_samples (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    order_no     TEXT        NOT NULL REFERENCES calibration_orders(order_no),
    sample_id    BIGINT      NOT NULL,
    temp_before  NUMERIC(10, 2) NOT NULL,
    temp_after   NUMERIC(10, 2) NOT NULL,
    ph_before    NUMERIC(5, 2)  NOT NULL,  -- pH 不参与换算，仅留痕
    ph_after     NUMERIC(5, 2)  NOT NULL,
    UNIQUE (order_no, sample_id),
    -- 一个编号一生只能被成功校正一次：删除后复用 id 也不能再次校正
    UNIQUE (sample_id)
);

CREATE INDEX IF NOT EXISTS idx_calibration_order_samples_order
    ON calibration_order_samples (order_no);
