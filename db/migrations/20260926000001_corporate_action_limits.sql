-- migrate:up
-- TWT49U 的官方漲跌停價與開盤競價基準：用來和除權息生效日的成交價交叉檢查（radar/audit.py）。
-- 含現金增資的除權，漲跌停以「減除股利參考價」為基準，不能用除權息參考價 × 1.1 推算，所以直接存官方數字。
ALTER TABLE corporate_actions
    ADD COLUMN limit_up   numeric(12, 2),   -- 漲停價格
    ADD COLUMN limit_down numeric(12, 2),   -- 跌停價格
    ADD COLUMN open_ref   numeric(12, 2);   -- 開盤競價基準

-- migrate:down
ALTER TABLE corporate_actions DROP COLUMN limit_up, DROP COLUMN limit_down, DROP COLUMN open_ref;
