-- migrate:up
-- 新聞與開盤快照。
--
-- news_items：新聞網站公開的 RSS（Yahoo 股市、中央社財經），只存標題、連結、時間，不存全文（全文有著作權，網頁上點標題連回原網站）。
--   stock_id 是 NULL＝大盤／財經新聞；有值＝那檔股票的新聞。同一則新聞可能同時出現在大盤與好幾檔個股。
-- open_snapshots：每天開盤後抓一次的盤中報價（證交所基本市況報導網站），只給系統判斷用，不對外顯示：
--   即時行情對外提供要先取得證交所授權，所以網站的唯讀帳號（radar_readonly）沒有讀取權限。

CREATE TABLE news_items (
    id              bigserial PRIMARY KEY,
    feed            text        NOT NULL,     -- yahoo_market／cna_finance／yahoo_stock
    stock_id        int         REFERENCES stocks (id),
    title           text        NOT NULL,
    url             text        NOT NULL,
    published_at    timestamptz NOT NULL,
    fetched_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE NULLS NOT DISTINCT (url, stock_id)
);
CREATE INDEX news_items_published ON news_items (published_at DESC);
CREATE INDEX news_items_stock ON news_items (stock_id, published_at DESC);

CREATE TABLE open_snapshots (
    trade_date      date        NOT NULL,
    symbol          text        NOT NULL,     -- 股票代號；加權指數 t00、櫃買指數 o00
    captured_at     timestamptz NOT NULL,
    quote_time      timestamptz,              -- 報價本身的時間
    price           numeric(12, 2),
    prev_close      numeric(12, 2),
    open            numeric(12, 2),
    high            numeric(12, 2),
    low             numeric(12, 2),
    volume_lots     bigint,                   -- 累計成交量（張）
    PRIMARY KEY (trade_date, symbol)
);

GRANT SELECT ON news_items TO radar_readonly;
-- init_schema 設了 ALTER DEFAULT PRIVILEGES，新表會自動給網站讀取權限；開盤快照要明確收回
REVOKE ALL ON open_snapshots FROM radar_readonly;

-- migrate:down
DROP TABLE IF EXISTS open_snapshots;
DROP TABLE IF EXISTS news_items;
