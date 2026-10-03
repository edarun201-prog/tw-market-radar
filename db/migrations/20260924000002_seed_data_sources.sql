-- migrate:up
INSERT INTO data_sources (code, name, url, is_official, license_note) VALUES
    ('TWSE', '臺灣證券交易所', 'https://www.twse.com.tw', true,
     '盤後公開報表，免費；有請求頻率限制。對外公開或商業使用前，需確認證交所資訊使用規範。'),
    ('TPEX', '證券櫃檯買賣中心', 'https://www.tpex.org.tw', true,
     '盤後公開報表，免費；有請求頻率限制。對外公開或商業使用前，需確認櫃買中心使用規範。'),
    ('FINMIND', 'FinMind', 'https://finmindtrade.com', false,
     '第三方彙整資料；免費層有次數限制，部分資料集需付費。僅作備援來源。')
ON CONFLICT (code) DO NOTHING;

-- migrate:down
DELETE FROM data_sources WHERE code IN ('TWSE', 'TPEX', 'FINMIND');
