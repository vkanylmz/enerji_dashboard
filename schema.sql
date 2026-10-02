-- Enerji Dashboard - Veritabanı Şeması (SQLite)
-- ================================================
-- Üç katman: companies (şirket) -> plants (santral) -> hourly_production (saatlik üretim)
-- Ayrıca hourly_ptf: piyasa fiyatı (tüm santraller için ortak, saatlik)

PRAGMA foreign_keys = ON;

-- Şirketler (ENERJİ SEKTÖRÜ + rapor PDF'inden)
CREATE TABLE IF NOT EXISTS companies (
    code                TEXT PRIMARY KEY,      -- örn. 'AKFYE'
    name                TEXT NOT NULL,          -- örn. 'Akfen Yenilenebilir Enerji'
    market_cap_mtl      REAL,                   -- Piyasa Değeri (mn TL)
    net_debt_mtl        REAL,                   -- Net Borç (mn TL)
    enterprise_value_mtl REAL,                  -- Firma Değeri (mn TL)
    ebitda_mtl          REAL,                   -- FAVÖK (mn TL)
    equity_mtl          REAL,                   -- Özkaynaklar (mn TL)
    installed_capacity_mw REAL,                 -- Toplam Kurulu Güç (MW)
    yekdem_ratio_pct    REAL,                   -- YEKDEM / Kurulu Güç oranı (%)
    ges_mw REAL, res_mw REAL, hes_mw REAL, bes_mw REAL, jes_mw REAL,
    dogalgaz_mw REAL, termik_mw REAL,
    data_source         TEXT,                   -- 'excel' | 'analyst_report'
    report_date         TEXT,                   -- rapor tarihi (varsa)
    updated_at          TEXT DEFAULT (datetime('now'))
);

-- Santraller (Sayfa1 + şirket sayfalarından)
CREATE TABLE IF NOT EXISTS plants (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    company_code        TEXT NOT NULL REFERENCES companies(code),
    name                TEXT NOT NULL,
    installed_capacity_mw REAL,
    eic_code             TEXT,                  -- EPİAŞ EIC kodu (varsa)
    excel_id             TEXT,                  -- Excel'deki ID kolonu (varsa, referans için)
    sale_type            TEXT,                  -- 'PTF' | 'YEKDEM'
    tariff_usd_cents      REAL,                  -- YEKDEM tarifesi (USD cent/kWh), sale_type=YEKDEM ise
    contract_end_year     TEXT,
    plant_type            TEXT,                  -- 'GES','RES','HES','JES','BES','Termik','Doğalgaz' vb.
    -- EPİAŞ'tan otomatik veri çekebilmek için gereken kimlikler (resolve_plant_ids.py ile doldurulur)
    epias_pp_id           INTEGER,
    epias_uevcb_id         INTEGER,
    epias_org_id           INTEGER,
    has_production_data    INTEGER DEFAULT 0,    -- Excel'de "ÜRETİM VERİSİ: X" işaretliyse 1
    id_resolved            INTEGER DEFAULT 0,    -- epias_* alanları doğrulanmışsa 1
    source                TEXT DEFAULT 'excel',  -- 'excel' | 'epias_discovered' (resolve script'in bulduğu eksik santral)
    is_active              INTEGER DEFAULT 1,
    notes                  TEXT,
    created_at             TEXT DEFAULT (datetime('now')),
    UNIQUE(company_code, name)
);

CREATE INDEX IF NOT EXISTS idx_plants_company ON plants(company_code);
CREATE INDEX IF NOT EXISTS idx_plants_eic ON plants(eic_code);

-- Saatlik PTF / SMF fiyatı (tüm santraller için ortak piyasa fiyatı)
CREATE TABLE IF NOT EXISTS hourly_price (
    dt_utc3             TEXT PRIMARY KEY,        -- 'YYYY-MM-DD HH:00' (Türkiye saati, UTC+3)
    ptf_try              REAL,
    ptf_usd               REAL,
    usdtry                 REAL,
    smf_try                 REAL
);

-- Santral bazlı saatlik üretim (UEVM/KGÜP'ten)
CREATE TABLE IF NOT EXISTS hourly_production (
    plant_id             INTEGER NOT NULL REFERENCES plants(id),
    dt_utc3               TEXT NOT NULL,          -- 'YYYY-MM-DD HH:00'
    production_mwh          REAL NOT NULL,
    data_source              TEXT,                 -- 'uevm' | 'kgup' | 'realtime'
    fetched_at                TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (plant_id, dt_utc3)
);

CREATE INDEX IF NOT EXISTS idx_prod_dt ON hourly_production(dt_utc3);

-- Çekme işleminin günlük çalışma kaydı (izleme / hata ayıklama için)
CREATE TABLE IF NOT EXISTS fetch_log (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at                 TEXT DEFAULT (datetime('now')),
    target_date             TEXT,                  -- hangi günün verisi çekildi
    plant_id                 INTEGER,
    status                    TEXT,                  -- 'ok' | 'error' | 'no_data'
    message                   TEXT
);

-- Türkiye geneli çeyreklik üretim/tüketim/PTF (ELEKTRİK verileri + PTF sayfalarından)
CREATE TABLE IF NOT EXISTS national_quarterly (
    quarter              TEXT PRIMARY KEY,     -- '1Q25', '2Q25', ...
    dogalgaz_mwh REAL, barajli_mwh REAL, linyit_mwh REAL, akarsu_mwh REAL,
    ithal_komur_mwh REAL, ruzgar_mwh REAL, gunes_mwh REAL, jeotermal_mwh REAL,
    diger_mwh REAL,
    toplam_uretim_mwh REAL,
    toplam_tuketim_mwh REAL,
    ptf_ort_try REAL,
    ptf_ort_usd REAL
);

-- --------------------------------------------------------------------------
-- Faydalı görünümler (view) - dashboard sorguları bunları kullanabilir
-- --------------------------------------------------------------------------

-- Santral bazlı günlük özet: toplam üretim + üretim-ağırlıklı ortalama PTF
DROP VIEW IF EXISTS v_plant_daily;
CREATE VIEW v_plant_daily AS
SELECT
    p.id AS plant_id,
    p.company_code,
    p.name AS plant_name,
    substr(hp.dt_utc3, 1, 10) AS gun,
    SUM(hp.production_mwh) AS toplam_uretim_mwh,
    ROUND(SUM(hp.production_mwh * pr.ptf_try) / NULLIF(SUM(hp.production_mwh), 0), 2) AS ptf_agirlikli_ort_try,
    ROUND(SUM(hp.production_mwh * pr.ptf_usd) / NULLIF(SUM(hp.production_mwh), 0), 2) AS ptf_agirlikli_ort_usd,
    ROUND(SUM(hp.production_mwh * pr.ptf_usd), 2) AS tahmini_gelir_usd
FROM hourly_production hp
JOIN plants p ON p.id = hp.plant_id
LEFT JOIN hourly_price pr ON pr.dt_utc3 = hp.dt_utc3
GROUP BY p.id, gun;

-- Şirket bazlı günlük özet (tüm santralleri toplar)
DROP VIEW IF EXISTS v_company_daily;
CREATE VIEW v_company_daily AS
SELECT
    company_code,
    gun,
    SUM(toplam_uretim_mwh) AS toplam_uretim_mwh,
    ROUND(SUM(toplam_uretim_mwh * ptf_agirlikli_ort_try) / NULLIF(SUM(toplam_uretim_mwh), 0), 2) AS ptf_agirlikli_ort_try,
    ROUND(SUM(tahmini_gelir_usd), 2) AS tahmini_gelir_usd
FROM v_plant_daily
GROUP BY company_code, gun;
