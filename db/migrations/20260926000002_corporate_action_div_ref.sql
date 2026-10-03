-- migrate:up
-- 減除股利參考價 ＝（前收盤 − 息值）÷（1 ＋ 無償配股率），不含現金增資。
-- 含現金增資的除權，市場當天的參考基準是這個價格（開盤競價基準依它取檔位），股價不會依「除權息參考價」跳空，
-- 所以還原價要用它：用除權息參考價會把天瀚 2026-08-18 算成單日 +62%（實際 +9.9%）。
ALTER TABLE corporate_actions ADD COLUMN div_ref numeric(12, 2);

-- migrate:down
ALTER TABLE corporate_actions DROP COLUMN div_ref;
