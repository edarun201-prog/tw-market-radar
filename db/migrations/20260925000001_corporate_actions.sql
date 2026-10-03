-- migrate:up
-- 除權息（TWT49U 除權除息計算結果表）。
-- Step 4 用來排除除權息造成的價格跳空：還原係數 = ref_price / prev_close。

CREATE TABLE corporate_actions (
    stock_id     int      NOT NULL REFERENCES stocks (id),
    ex_date      date     NOT NULL,
    action_type  text     NOT NULL CHECK (action_type IN ('dividend', 'rights', 'both')),  -- 息 / 權 / 權息
    prev_close   numeric(12, 2) NOT NULL,   -- 除權息前收盤價
    ref_price    numeric(12, 2) NOT NULL,   -- 除權息參考價
    value        numeric(12, 4) NOT NULL,   -- 權值＋息值（元）
    source_id    smallint NOT NULL REFERENCES data_sources (id),
    ingested_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (stock_id, ex_date)
);
CREATE INDEX corporate_actions_ex_date_idx ON corporate_actions (ex_date);

GRANT SELECT ON corporate_actions TO radar_readonly;

-- migrate:down
DROP TABLE IF EXISTS corporate_actions;
