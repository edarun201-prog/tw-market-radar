-- migrate:up
-- 網站用的唯讀登入帳號：只繼承 radar_readonly 的 SELECT 權限，網站程式有錯也改不到資料。
-- 資料庫只接受本機連線（db/native/setup.sql 的 listen_addresses），這組開發用密碼與 .env.example 相同。
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'radar_web') THEN
        CREATE ROLE radar_web LOGIN PASSWORD 'radar_web' IN ROLE radar_readonly;
    END IF;
END
$$;
GRANT CONNECT ON DATABASE radar TO radar_web;

-- migrate:down
DROP ROLE IF EXISTS radar_web;
