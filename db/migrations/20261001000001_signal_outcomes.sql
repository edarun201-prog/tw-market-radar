-- migrate:up
-- 訊號回測（Phase 3）：每一筆訊號出現後的還原報酬，以及同一天「全部普通股」的報酬（比較基準）。
-- 報酬都從訊號當天收盤算起（h5／h20／h60）；d20 是「盤後才看到訊號、隔天收盤才買到」的 20 日報酬。
-- e5／e20／e60／ed20 是各段報酬結束的交易日：匯出過去日期的快照時，只用那天之前已經知道結果的訊號。
-- NULL 表示那一天之後還沒有滿 N 個交易日。每天由 radar/outcomes.py 整批重算，結果相同。

CREATE TABLE market_forward_returns (
    trade_date  date     NOT NULL,
    horizon     text     NOT NULL,          -- h5、h20、h60、d20
    n           int      NOT NULL,          -- 有算到報酬的股票數
    mean        numeric(10, 6),             -- 等權平均
    median      numeric(10, 6),
    up_share    numeric(6, 4),              -- 上漲的比例
    PRIMARY KEY (trade_date, horizon)
);

CREATE TABLE signal_outcomes (
    signal_id   bigint PRIMARY KEY REFERENCES market_signals (id) ON DELETE CASCADE,
    h5          numeric(10, 6),
    h20         numeric(10, 6),
    h60         numeric(10, 6),
    d20         numeric(10, 6),
    b5          numeric(10, 6),             -- 同一天全部普通股的平均報酬（比較基準）
    b20         numeric(10, 6),
    b60         numeric(10, 6),
    bd20        numeric(10, 6),
    e5          date,
    e20         date,
    e60         date,
    ed20        date,
    computed_at timestamptz NOT NULL DEFAULT now()
);

GRANT SELECT ON market_forward_returns, signal_outcomes TO radar_readonly;

-- migrate:down
DROP TABLE IF EXISTS signal_outcomes;
DROP TABLE IF EXISTS market_forward_returns;
