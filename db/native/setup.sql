-- 本機 PostgreSQL（不用 Docker）第一次設定，用 postgres 超級使用者執行一次：
--   & "C:\Program Files\PostgreSQL\18\bin\psql.exe" -U postgres -f db\native\setup.sql
-- （16 以上皆可，路徑中的版本號換成實際安裝的版本）
-- 會要求輸入安裝時設定的 postgres 密碼。重複執行不會出錯。

-- 中文版 Windows 的 psql 預設用 BIG5 讀檔，這個檔案是 UTF-8，要先指定
\encoding UTF8

-- 專案帳號與資料庫 ------------------------------------------------------
-- 帳號密碼與 .env.example 相同；下方 listen_addresses 限制只接受本機連線。
-- CREATEROLE：migration 要建立 radar_readonly。
-- （DO 區塊裡的內容會原樣送到伺服器，不要在裡面寫中文）
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'radar') THEN
        CREATE ROLE radar LOGIN PASSWORD 'radar' CREATEROLE;
    END IF;
END
$$;
SELECT 'CREATE DATABASE radar OWNER radar' WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'radar')\gexec
SELECT 'CREATE DATABASE radar_test OWNER radar' WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'radar_test')\gexec

-- 低記憶體設定（4 GB 筆電）----------------------------------------------
-- 一年資料約 60 萬列、幾百 MB，這些值綽綽有餘；記憶體升級到 8 GB 後可把 effective_cache_size 調成 3GB。
ALTER SYSTEM SET max_connections = 20;          -- 預設 100；只有管線和網站在用
ALTER SYSTEM SET shared_buffers = '128MB';
ALTER SYSTEM SET effective_cache_size = '1GB';  -- 只是給查詢規劃器的估計值，不會實際占用
ALTER SYSTEM SET work_mem = '8MB';              -- 特徵計算的視窗函數會用到排序
ALTER SYSTEM SET maintenance_work_mem = '64MB';
ALTER SYSTEM SET jit = off;                     -- 小查詢用 JIT 反而更慢、更吃記憶體
ALTER SYSTEM SET listen_addresses = 'localhost';
ALTER SYSTEM SET timezone = 'Asia/Taipei';
SELECT pg_reload_conf();
-- max_connections、shared_buffers、listen_addresses 要重新啟動 PostgreSQL 服務（或重開機）才生效。
