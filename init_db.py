"""
Veritabanını kurar ve Excel dosyasındaki (Enerji_Sektörü.xlsx) şirket + santral
listesini otomatik olarak SQLite'a yükler.

Kaynaklar:
  - 'Sayfa1' sayfası  -> ana santral listesi (Şirket, Santral, Kurulu Güç, Satış Türü, Üretim Verisi)
  - Şirket sayfaları (AKFYE, ZOREN, ...) -> EIC kodu, ID, satış fiyatı, süre (varsa)
  - 'ENERJİ SEKTÖRÜ' sayfası -> şirket bazlı kurulu güç / GES-RES-HES kırılımı (finansal kolonlar
    Excel'de kırık olduğu için buradan alınmıyor; onlar analist raporundan elle girilir, bkz.
    load_report_financials() fonksiyonu)

Çalıştırma:
    python scripts/init_db.py --excel "/path/to/Enerji_Sektörü.xlsx" --db data/energy.db
"""

import argparse
import re
import sqlite3
from pathlib import Path

import openpyxl

BASE_DIR = Path(__file__).resolve().parent.parent
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def create_schema(conn):
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        conn.executescript(f.read())
    conn.commit()
    print("✓ Şema oluşturuldu (companies, plants, hourly_price, hourly_production, ...)")


def guess_plant_type(name):
    name_l = name.lower()
    if "ges" in name_l or "güneş" in name_l or "gunes" in name_l:
        return "GES"
    if "res" in name_l or "rüzgar" in name_l:
        return "RES"
    if "hes" in name_l or "baraj" in name_l or "akarsu" in name_l:
        return "HES"
    if "jes" in name_l or "jeotermal" in name_l:
        return "JES"
    if "bes" in name_l or "depolama" in name_l:
        return "BES"
    if "termik" in name_l or "kömür" in name_l:
        return "Termik"
    if "doğalgaz" in name_l or "dogalgaz" in name_l or "çevrim" in name_l:
        return "Doğalgaz"
    return None


def load_plant_roster(conn, wb):
    """Sayfa1'deki ana listeyi companies + plants tablolarına yükler."""
    ws = wb["Sayfa1"]
    current_company = None
    inserted_companies = set()
    n_plants = 0

    cur = conn.cursor()

    for row in ws.iter_rows(min_row=2, values_only=True):
        sirket, santral, kurulu_guc, satis_turu, uretim_var = (row + (None,) * 5)[:5]

        if sirket:
            current_company = str(sirket).strip()

        if not santral or str(santral).strip().lower() == "toplam":
            continue
        if current_company is None:
            continue

        santral = str(santral).strip()

        if current_company not in inserted_companies:
            cur.execute(
                "INSERT OR IGNORE INTO companies (code, name, data_source) VALUES (?, ?, 'excel')",
                (current_company, current_company),
            )
            inserted_companies.add(current_company)

        has_data = 1 if (uretim_var and str(uretim_var).strip().upper() == "X") else 0

        cur.execute(
            """
            INSERT OR IGNORE INTO plants
                (company_code, name, installed_capacity_mw, sale_type, plant_type, has_production_data, source)
            VALUES (?, ?, ?, ?, ?, ?, 'excel')
            """,
            (
                current_company,
                santral,
                float(kurulu_guc) if isinstance(kurulu_guc, (int, float)) else None,
                str(satis_turu).strip() if satis_turu else None,
                guess_plant_type(santral),
                has_data,
            ),
        )
        n_plants += 1

    conn.commit()
    print(f"✓ Sayfa1'den {len(inserted_companies)} şirket, {n_plants} santral satırı işlendi")


def enrich_from_company_sheets(conn, wb):
    """Şirket sayfalarındaki EIC kodu / satış fiyatı / süre bilgisiyle plants tablosunu zenginleştirir."""
    company_codes = {r[0] for r in conn.execute("SELECT code FROM companies")}
    cur = conn.cursor()
    n_updated = 0

    for sheet_name in wb.sheetnames:
        if sheet_name not in company_codes:
            continue
        ws = wb[sheet_name]

        for row in ws.iter_rows(min_row=2, max_col=6, values_only=True):
            santral, kurulu_guc, eic, excel_id, satis_fiyati, sure = (row + (None,) * 6)[:6]
            if not santral:
                continue
            santral = str(santral).strip()
            if santral.lower() == "toplam":
                continue

            eic_val = str(eic).strip() if eic else None
            excel_id_val = str(excel_id).strip() if excel_id else None

            tariff = None
            sale_type = None
            if satis_fiyati is not None:
                s = str(satis_fiyati).strip()
                if s.upper() == "PTF":
                    sale_type = "PTF"
                else:
                    # YEKDEM tarifesi genelde sayı (USD cent/kWh), örn 73, 133
                    m = re.match(r"^-?\d+(\.\d+)?$", s)
                    if m:
                        tariff = float(s)
                        sale_type = "YEKDEM"

            cur.execute(
                """
                UPDATE plants
                SET eic_code = COALESCE(?, eic_code),
                    excel_id = COALESCE(?, excel_id),
                    tariff_usd_cents = COALESCE(?, tariff_usd_cents),
                    sale_type = COALESCE(sale_type, ?),
                    contract_end_year = COALESCE(?, contract_end_year)
                WHERE company_code = ? AND name = ?
                """,
                (
                    eic_val, excel_id_val, tariff, sale_type,
                    str(sure).strip() if sure else None,
                    sheet_name, santral,
                ),
            )
            if cur.rowcount:
                n_updated += 1

    conn.commit()
    print(f"✓ Şirket sayfalarından {n_updated} santral satırı EIC/fiyat bilgisiyle zenginleştirildi")


def load_report_financials(conn):
    """
    2Q26 analist raporundaki (PDF) doğrulanmış finansal + kapasite tablosu.
    Excel'deki #NAME?/#REF! hatalı kolonların yerine bu veriler kullanılıyor.
    Kaynak: Enerji_Sektoru_2Q26.pdf, 16.07.2026 kapanış verileri.
    """
    # (kod, piyasa_degeri, net_borc, firma_degeri, favok, favok_usd, net_borc_favok, ozkaynaklar,
    #  kurulu_guc, ges, res, hes, bes, jes, dogalgaz, termik, yekdem_oran)
    rows = [
        ("A1YEN", 1842, 299, 2141, 300, 6, 1.00, 3376, 53, 53, None, None, None, None, None, None, 100),
        ("AKENR", 8028, 23382, 31410, 880, 19, 26.57, 14553, 1224, None, 28, 292, None, None, 904, None, 0),
        ("AKFYE", 29206, 11367, 40573, 3206, 68, 3.55, 62457, 887, 121, 536, 229, None, None, None, None, 75),
        ("AKSEN", 125945, 52920, 178864, 11885, 253, 4.45, 70028, 3188, None, None, None, None, None, 2300, 888, 8),
        ("AKSUE", 2930, 0, 2931, 19, 0, 0.01, 990, 20, 9, None, 13, None, None, None, None, 17),
        ("AYDEM", 19134, 22054, 41187, 4831, 103, 4.57, 41071, 1198, None, 339, 852, None, 7, None, None, 28),
        ("AYEN", 9907, 6413, 16320, 2192, 47, 2.93, 19481, 312, None, 134, 179, None, None, None, None, 9),
        ("BIGEN", 60931, 3185, 64116, 689, 15, 4.62, 3343, 146, 146, None, None, None, None, None, None, 41),
        ("BIOEN", 8870, 5734, 14604, 249, 5, 23.07, 3275, 114, None, None, None, 114, None, None, None, 92),
        ("CANTE", 13000, -1469, 11531, 2111, 45, -0.70, 32221, 330, None, None, None, None, None, None, 330, 0),
        ("CATES", 7550, -897, 6653, 386, 8, -2.32, 13277, 315, None, None, None, None, None, None, 315, 0),
        ("CONSE", 1958, 3913, 5872, 350, 7, 11.18, 2848, 111, 11, None, None, 34, None, None, None, 40),
        ("ECOGR", 20909, 8256, 29165, 1911, 41, 4.32, 18418, 179, 157, None, None, 22, None, None, None, 25),
        ("ENDAE", 7301, -1478, 5823, 573, 12, -2.58, 15692, 189, 9, 68, 104, None, 7, None, None, 45),
        ("ENTRA", 18869, 11619, 30488, 2906, 62, 4.00, 38509, 488, 100, None, 388, None, None, None, None, 0),
        ("GWIND", 13349, 1593, 14942, 1918, 41, 0.83, 15689, 354, 70, 284, None, None, None, None, None, 42),
        ("IZENR", 21676, 325, 22001, 1735, 37, 0.19, 18078, 513, 143, None, None, None, None, None, 370, 0),
        ("LYDYE", 25723, -25, 25698, 17, 0, -1.48, 3280, 6, 6, None, None, None, None, None, None, 0),
        ("MOGAN", 40896, 30095, 70991, 7059, 150, 4.26, 97804, 973, 33, 601, 79, None, 260, None, None, 44),
        ("PAMEL", 2582, -4, 2578, 9, 0, -0.45, 666, 18, 4, None, 14, None, None, None, None, 0),
        ("TATEN", 13958, 6973, 20931, 1646, 35, 4.24, 8297, 363, 82, 259, 22, None, None, None, None, 100),
        ("ZEDUR", 1526, 464, 1989, 22, 0, 21.14, 2448, 16, 12, None, 4, None, None, None, None, 16),
        ("ZOREN", 13400, 55179, 68579, 6162, 131, 8.95, 61080, 531, 2, 56, 168, None, 305, 50, None, 31),
    ]

    cur = conn.cursor()
    for r in rows:
        (code, mcap, netdebt, ev, ebitda, ebitda_usd, netdebt_ebitda, equity,
         capacity, ges, res, hes, bes, jes, dogalgaz, termik, yekdem) = r
        cur.execute(
            """
            UPDATE companies SET
                market_cap_mtl = ?, net_debt_mtl = ?, enterprise_value_mtl = ?,
                ebitda_mtl = ?, equity_mtl = ?, installed_capacity_mw = ?,
                yekdem_ratio_pct = ?, ges_mw = ?, res_mw = ?, hes_mw = ?, bes_mw = ?,
                jes_mw = ?, dogalgaz_mw = ?, termik_mw = ?,
                data_source = 'analyst_report_2Q26', report_date = '2026-07-17'
            WHERE code = ?
            """,
            (mcap, netdebt, ev, ebitda, equity, capacity, yekdem, ges, res, hes, bes,
             jes, dogalgaz, termik, code),
        )
        if cur.rowcount == 0:
            # Excel'deki Sayfa1'de yoksa (örn. ALFAS, CWENE, HUNER) şirketi de ekleyelim
            cur.execute(
                "INSERT OR IGNORE INTO companies (code, name, data_source, report_date) VALUES (?, ?, 'analyst_report_2Q26', '2026-07-17')",
                (code, code),
            )
            cur.execute(
                """
                UPDATE companies SET
                    market_cap_mtl = ?, net_debt_mtl = ?, enterprise_value_mtl = ?,
                    ebitda_mtl = ?, equity_mtl = ?, installed_capacity_mw = ?,
                    yekdem_ratio_pct = ?, ges_mw = ?, res_mw = ?, hes_mw = ?, bes_mw = ?,
                    jes_mw = ?, dogalgaz_mw = ?, termik_mw = ?
                WHERE code = ?
                """,
                (mcap, netdebt, ev, ebitda, equity, capacity, yekdem, ges, res, hes, bes,
                 jes, dogalgaz, termik, code),
            )
    conn.commit()
    print(f"✓ Analist raporundan {len(rows)} şirket için finansal/kapasite verisi yüklendi")


def load_national_quarterly(conn):
    """Türkiye geneli çeyreklik üretim/tüketim/PTF (ELEKTRİK verileri + PTF + PDF'ten)."""
    # (çeyrek, dogalgaz, barajli, linyit, akarsu, ithal_komur, ruzgar, gunes, jeotermal,
    #  diger, toplam_uretim, toplam_tuketim, ptf_try, ptf_usd)
    rows = [
        ("1Q25", 20947165, 10582363, 8987910, 3750644, 19641395, 9314261, 1294756, 2887916,
         2610359, 80288430, 85439509, 2417.98, 66.98),   # ptf_try/usd = çeyrek ort. (3 aylık basit ort.)
        ("2Q25", 12220441, 12191756, 8995259, 7283645, 14251331, 9380815, 2307057, 2572269,
         2038108, 73240681, 82538168, 2401.61, 62.17),
        ("3Q25", 23756049, 10859817, 10452822, 2543310, 21191763, 11884244, 2682437, 2336947,
         3642162, 89349551, 99322758, 2837.19, 71.33),
        ("4Q25", 21440711, 7743442, 10424847, 2260629, 21028349, 8666695, 1448492, 2850593,
         3736661, 79600419, 83355495, 2861.65, 67.89),
        ("1Q26", 12712375, 16740874, 9475904, 6269045, 17066861, 12870144, 1349259, 2842540,
         3009947, 82335949, 87905548, 2266.06, 52.14),
        ("2Q26", 7337653, 25603665, 8690906, 8208169, 6700763, 8401529, 2249673, 2584530,
         3087568, 72864456, 83244799, 1068.54, 23.54),
    ]
    cur = conn.cursor()
    for r in rows:
        cur.execute(
            """
            INSERT INTO national_quarterly
                (quarter, dogalgaz_mwh, barajli_mwh, linyit_mwh, akarsu_mwh, ithal_komur_mwh,
                 ruzgar_mwh, gunes_mwh, jeotermal_mwh, diger_mwh, toplam_uretim_mwh,
                 toplam_tuketim_mwh, ptf_ort_try, ptf_ort_usd)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(quarter) DO UPDATE SET
                toplam_uretim_mwh=excluded.toplam_uretim_mwh,
                toplam_tuketim_mwh=excluded.toplam_tuketim_mwh
            """,
            r,
        )
    conn.commit()
    print(f"✓ Türkiye geneli {len(rows)} çeyreklik veri yüklendi")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--excel", required=True, help="Enerji_Sektörü.xlsx dosya yolu")
    parser.add_argument("--db", default=str(BASE_DIR / "data" / "energy.db"))
    args = parser.parse_args()

    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(args.db)

    create_schema(conn)

    print(f"\nExcel okunuyor: {args.excel}")
    wb = openpyxl.load_workbook(args.excel, read_only=True, data_only=True)

    load_plant_roster(conn, wb)
    enrich_from_company_sheets(conn, wb)
    load_report_financials(conn)
    load_national_quarterly(conn)

    n_companies = conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
    n_plants = conn.execute("SELECT COUNT(*) FROM plants").fetchone()[0]
    n_with_eic = conn.execute("SELECT COUNT(*) FROM plants WHERE eic_code IS NOT NULL").fetchone()[0]
    n_with_data = conn.execute("SELECT COUNT(*) FROM plants WHERE has_production_data = 1").fetchone()[0]

    print(f"\n=== Özet ===")
    print(f"Şirket sayısı        : {n_companies}")
    print(f"Santral sayısı       : {n_plants}")
    print(f"EIC kodu olan        : {n_with_eic} / {n_plants}")
    print(f"Üretim verisi olan   : {n_with_data} / {n_plants}  (bunlar dışındakiler resolve_plant_ids.py + EPİAŞ ile tamamlanmalı)")
    print(f"\nVeritabanı: {args.db}")

    conn.close()


if __name__ == "__main__":
    main()
