-- migrate:up
-- AI 台股市場雷達：初始 schema
-- 單位規則：量＝股、金額＝元；顯示層再換算成張、億元。

CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- 資料來源 -------------------------------------------------------------
CREATE TABLE data_sources (
    id            smallserial PRIMARY KEY,
    code          text        NOT NULL UNIQUE,          -- TWSE / TPEX / FINMIND
    name          text        NOT NULL,
    url           text,
    is_official   boolean     NOT NULL DEFAULT true,
    license_note  text,
    created_at    timestamptz NOT NULL DEFAULT now()
);

-- 產業別（官方分類，Step 4 由公司基本資料填入）-------------------------
CREATE TABLE industries (
    id      serial PRIMARY KEY,
    market  text NOT NULL CHECK (market IN ('TWSE', 'TPEX')),
    code    text NOT NULL,
    name    text NOT NULL,
    UNIQUE (market, code)
);

-- 證券主檔 -------------------------------------------------------------
CREATE TABLE stocks (
    id               serial PRIMARY KEY,
    market           text    NOT NULL CHECK (market IN ('TWSE', 'TPEX')),
    symbol           text    NOT NULL,
    name             text    NOT NULL,
    industry_id      int     REFERENCES industries (id),
    security_type    text    NOT NULL DEFAULT 'stock'
                     CHECK (security_type IN ('stock', 'etf', 'other')),
    listed_date      date,
    first_seen_date  date,
    last_seen_date   date,
    is_active        boolean NOT NULL DEFAULT true,
    updated_at       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (market, symbol)
);
CREATE INDEX stocks_name_trgm_idx   ON stocks USING gin (name gin_trgm_ops);
CREATE INDEX stocks_symbol_prefix_idx ON stocks (symbol text_pattern_ops);

-- 交易日曆（由資料抓取結果自動建立）-----------------------------------
CREATE TABLE trading_calendar (
    trade_date  date PRIMARY KEY,
    is_open     boolean NOT NULL,
    note        text,
    checked_at  timestamptz NOT NULL DEFAULT now()
);

-- 日行情（價格＋成交量合併）-------------------------------------------
CREATE TABLE daily_prices (
    stock_id        int      NOT NULL REFERENCES stocks (id),
    trade_date      date     NOT NULL REFERENCES trading_calendar (trade_date),
    open            numeric(12, 2),
    high            numeric(12, 2),
    low             numeric(12, 2),
    close           numeric(12, 2),
    change          numeric(12, 2),
    is_no_compare   boolean  NOT NULL DEFAULT false,  -- 官方標記 X（不比價，常見於除權息日）
    volume          bigint   NOT NULL DEFAULT 0,      -- 股
    turnover        bigint   NOT NULL DEFAULT 0,      -- 元
    trade_count     int,
    source_id       smallint NOT NULL REFERENCES data_sources (id),
    ingested_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (stock_id, trade_date)
);
CREATE INDEX daily_prices_trade_date_idx ON daily_prices (trade_date);

-- 三大法人買賣超（股）--------------------------------------------------
CREATE TABLE institutional_flows (
    stock_id      int      NOT NULL REFERENCES stocks (id),
    trade_date    date     NOT NULL REFERENCES trading_calendar (trade_date),
    foreign_buy   bigint,
    foreign_sell  bigint,
    foreign_net   bigint,
    trust_buy     bigint,
    trust_sell    bigint,
    trust_net     bigint,
    dealer_buy    bigint,
    dealer_sell   bigint,
    dealer_net    bigint,
    total_net     bigint,
    source_id     smallint NOT NULL REFERENCES data_sources (id),
    ingested_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (stock_id, trade_date)
);
CREATE INDEX institutional_flows_trade_date_idx ON institutional_flows (trade_date);

-- 預先計算的特徵值（Step 4 填入）---------------------------------------
CREATE TABLE daily_features (
    stock_id        int  NOT NULL,
    trade_date      date NOT NULL,
    vol_avg20       numeric(18, 2),
    vol_ratio       numeric(10, 4),
    turnover_avg20  numeric(20, 2),
    ret_1d          numeric(10, 6),
    ret_5d          numeric(10, 6),
    ret_20d         numeric(10, 6),
    high20          numeric(12, 2),
    low20           numeric(12, 2),
    high60          numeric(12, 2),
    low60           numeric(12, 2),
    has_ex_right    boolean NOT NULL DEFAULT false,
    computed_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (stock_id, trade_date),
    FOREIGN KEY (stock_id, trade_date) REFERENCES daily_prices (stock_id, trade_date) ON DELETE CASCADE
);
CREATE INDEX daily_features_ratio_idx ON daily_features (trade_date, vol_ratio DESC);

-- 異常訊號 -------------------------------------------------------------
CREATE TABLE market_signals (
    id              bigserial PRIMARY KEY,
    stock_id        int   NOT NULL REFERENCES stocks (id),
    trade_date      date  NOT NULL,
    signal_type     text  NOT NULL,
    value           numeric(18, 6),
    threshold       numeric(18, 6),
    evidence        jsonb NOT NULL DEFAULT '{}'::jsonb,
    engine_version  text  NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (stock_id, trade_date, signal_type, engine_version)
);
CREATE INDEX market_signals_date_type_idx ON market_signals (trade_date, signal_type);

-- 指數（含類股指數）---------------------------------------------------
CREATE TABLE market_indices (
    index_code  text NOT NULL,           -- 官方指數名稱，例：發行量加權股價指數
    trade_date  date NOT NULL,
    close       numeric(14, 2),
    change      numeric(14, 2),
    change_pct  numeric(8, 4),
    source_id   smallint NOT NULL REFERENCES data_sources (id),
    PRIMARY KEY (index_code, trade_date)
);

-- 市場廣度與總成交 -----------------------------------------------------
CREATE TABLE market_breadth (
    trade_date      date NOT NULL,
    market          text NOT NULL CHECK (market IN ('TWSE', 'TPEX')),
    advancers       int,
    decliners       int,
    unchanged       int,
    limit_up        int,
    limit_down      int,
    no_trade        int,
    total_volume    bigint,
    total_turnover  bigint,
    total_trades    bigint,
    source_id       smallint NOT NULL REFERENCES data_sources (id),
    PRIMARY KEY (trade_date, market)
);

-- 抓取紀錄（冪等、可續跑）---------------------------------------------
CREATE TABLE ingestion_runs (
    id             bigserial PRIMARY KEY,
    source_id      smallint NOT NULL REFERENCES data_sources (id),
    dataset        text     NOT NULL,
    target_date    date     NOT NULL,
    status         text     NOT NULL
                   CHECK (status IN ('running', 'success', 'no_data', 'pending', 'failed')),
    row_count      int,
    warning_count  int,
    warnings       text,                 -- 前 20 則警告
    error          text,
    raw_path       text,
    attempts       int      NOT NULL DEFAULT 1,
    started_at     timestamptz NOT NULL DEFAULT now(),
    finished_at    timestamptz,
    UNIQUE (source_id, dataset, target_date)
);

-- AI 每日摘要（Step 9）------------------------------------------------
CREATE TABLE market_summaries (
    trade_date  date PRIMARY KEY,
    content     text  NOT NULL,
    facts       jsonb NOT NULL DEFAULT '{}'::jsonb,
    provider    text,
    model       text,
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- AI 問答紀錄（Step 8）------------------------------------------------
CREATE TABLE ai_queries (
    id             bigserial PRIMARY KEY,
    created_at     timestamptz NOT NULL DEFAULT now(),
    question       text NOT NULL,
    answer         text,
    tool_calls     jsonb,
    as_of_date     date,
    provider       text,
    model          text,
    input_tokens   int,
    output_tokens  int,
    latency_ms     int,
    status         text
);
CREATE INDEX ai_queries_created_at_idx ON ai_queries (created_at);

-- 唯讀角色（網站用；登入帳號在 Step 5 建立）----------------------------
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'radar_readonly') THEN
        CREATE ROLE radar_readonly NOLOGIN;
    END IF;
END
$$;
GRANT USAGE ON SCHEMA public TO radar_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO radar_readonly;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO radar_readonly;

-- migrate:down
DROP TABLE IF EXISTS ai_queries, market_summaries, ingestion_runs, market_breadth,
    market_indices, market_signals, daily_features, institutional_flows, daily_prices,
    trading_calendar, stocks, industries, data_sources CASCADE;
