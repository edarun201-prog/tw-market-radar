-- migrate:up
-- 法人說明會（法說會）：上市、上櫃公司召開或受邀參加法說會前，要發重大訊息（符合條款第 12 款）。
-- 來源是證交所、櫃買中心 OpenAPI 的「每日重大訊息」，只提供最新一天，所以從 2026-11 起逐日累積。
--
-- 一則公告一列（同一家公司同一個發言時間）：更正公告會是新的一列，顯示時取同一場（同一家公司同一天）最新的那則。
-- 日期可能是區間（例：受邀參加券商舉辦的兩天論壇），單日的 ends_on = starts_on。

CREATE TABLE investor_conferences (
    stock_id        int      NOT NULL REFERENCES stocks (id),
    announced_on    date     NOT NULL,       -- 發言日期
    announced_at    time     NOT NULL,       -- 發言時間
    starts_on       date     NOT NULL,       -- 法說會日期（區間的第一天）
    ends_on         date     NOT NULL,       -- 區間的最後一天
    start_time      time,                    -- 開始時間；公告沒寫或寫法看不懂時為 NULL
    location        text,                    -- 地點（原文，可能是「線上法說會」）
    summary         text,                    -- 法人說明會擇要訊息（原文）
    subject         text     NOT NULL,       -- 公告主旨
    source_id       smallint NOT NULL REFERENCES data_sources (id),
    PRIMARY KEY (stock_id, announced_on, announced_at)
);
CREATE INDEX investor_conferences_starts_on ON investor_conferences (starts_on);

GRANT SELECT ON investor_conferences TO radar_readonly;

-- migrate:down
DROP TABLE IF EXISTS investor_conferences;
