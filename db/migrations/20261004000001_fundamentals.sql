-- migrate:up
-- 估值與財報（基本面）：都來自證交所、櫃買中心的官方 OpenAPI，只提供「最新一期」，所以從 2026-10 起逐日／逐季累積。
--
-- valuations：每天的本益比、殖利率、股價淨值比（官方計算）。本益比空白＝虧損或無法計算，存 NULL，不自己補。
-- financial_reports：綜合損益表的重點。官方數字是「年度累計」（第 2 季＝上半年合計），金額單位原本是千元，這裡換成元。
--   單季數字＝這一季累計 − 上一季累計，要兩季都有資料才算得出來，不另外存。

CREATE TABLE valuations (
    stock_id        int      NOT NULL REFERENCES stocks (id),
    trade_date      date     NOT NULL,
    pe_ratio        numeric(12, 2),          -- 本益比（倍）
    dividend_yield  numeric(8, 2),           -- 殖利率（%）
    pb_ratio        numeric(10, 2),          -- 股價淨值比（倍）
    source_id       smallint NOT NULL REFERENCES data_sources (id),
    PRIMARY KEY (stock_id, trade_date)
);

CREATE TABLE financial_reports (
    stock_id        int      NOT NULL REFERENCES stocks (id),
    fiscal_year     smallint NOT NULL,       -- 西元年
    quarter         smallint NOT NULL CHECK (quarter BETWEEN 1 AND 4),
    report_type     text     NOT NULL,       -- ci 一般業、basi 銀行、bd 證券期貨、fh 金控、ins 保險、mim 異業
    revenue         numeric(20, 0),          -- 營業收入（元，年度累計；銀行業沒有這一欄）
    net_income      numeric(20, 0),          -- 淨利歸屬於母公司業主（元，年度累計）
    eps             numeric(10, 2),          -- 基本每股盈餘（元，年度累計）
    published_on    date,                    -- 官方出表日期
    source_id       smallint NOT NULL REFERENCES data_sources (id),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (stock_id, fiscal_year, quarter)
);

GRANT SELECT ON valuations, financial_reports TO radar_readonly;

-- migrate:down
DROP TABLE IF EXISTS financial_reports;
DROP TABLE IF EXISTS valuations;
