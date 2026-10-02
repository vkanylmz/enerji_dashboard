"""
uevcb_manual_review.csv'nin yerine geçen, elle inceleme için daha kullanışlı
bir Excel üretir: "Elle_Eslestirme.xlsx".

resolve_plant_ids.py'deki AYNI eşleştirme mantığını (tam -> esnetilmiş ->
fuzzy) tekrar çalıştırır (salt-okunur, DB'ye YAZMAZ) ama bu sefer aday
UEVÇB'lerin id/isim/tip bilgisini de saklar (CSV'de sadece isim vardı).

Çalıştırma:
    python export_review_excel.py
"""

import sqlite3

import pandas as pd

from resolve_plant_ids import (
    BASE_DIR,
    DB_PATH,
    build_uevcb_catalog,
    extract_type_token,
    get_client,
    normalize_plant_name,
    relax_plant_name,
)
import difflib

OUT_PATH = BASE_DIR / "Elle_Eslestirme.xlsx"


def candidate_label(u):
    t = extract_type_token(normalize_plant_name(u.get("name"))) or "?"
    return f"{u.get('id')} — {u.get('name')} ({t})"


def classify_plant(key, our_type, strict_catalog, relaxed_catalog, strict_keys):
    """resolve_plant_ids.py'deki step2_resolve_company ile AYNI karar ağacı
    (tam -> esnetilmiş -> fuzzy), ama salt-okunur: (durum, aday_listesi) döner.
    'Eşleşti' durumu buraya hiç gelmez çünkü çağıran taraf zaten sadece
    epias_uevcb_id IS NULL olan (yani hâlâ çözülmemiş) santralleri geçiriyor."""
    candidates = strict_catalog.get(key)
    if candidates and len(candidates) > 1:
        return "Belirsiz", candidates
    if candidates and len(candidates) == 1:
        # Teorik olarak zaten çözülmüş olmalı (epias_uevcb_id dolu) — buraya
        # düşerse güvenlik payı olarak yine raporla, sessizce atlama.
        return "Belirsiz", candidates

    rkey = relax_plant_name(key)
    rcands = relaxed_catalog.get(rkey, [])
    if our_type:
        rcands = [c for c in rcands
                  if extract_type_token(normalize_plant_name(c.get("name"))) in (our_type, None)]
    if len(rcands) > 1:
        return "Belirsiz", rcands
    if len(rcands) == 1:
        return "Belirsiz", rcands  # aynı güvenlik payı

    close = difflib.get_close_matches(key, strict_keys, n=3, cutoff=0.82)
    fuzzy_cands = []
    for c in close:
        cand = strict_catalog[c][0]
        cand_type = extract_type_token(c)
        if our_type and cand_type and cand_type != our_type:
            continue
        fuzzy_cands.append(cand)
    if fuzzy_cands:
        return "Olası", fuzzy_cands

    return "Bulunamadı", []


def collect_review_rows(conn, strict_catalog, relaxed_catalog):
    strict_keys = list(strict_catalog.keys())
    rows = []

    companies = [r[0] for r in conn.execute("SELECT code FROM companies ORDER BY code")]
    for company_code in companies:
        plants = conn.execute(
            """SELECT name, installed_capacity_mw FROM plants
               WHERE company_code=? AND epias_uevcb_id IS NULL
               ORDER BY name""",
            (company_code,),
        ).fetchall()

        for name, capacity in plants:
            key = normalize_plant_name(name)
            our_type = extract_type_token(key)
            status, cands = classify_plant(key, our_type, strict_catalog, relaxed_catalog, strict_keys)

            rows.append({
                "Şirket": company_code,
                "Bizim santral adımız": name,
                "Bizim kurulu gücümüz (MW)": capacity,
                "Durum": status,
                "Aday 1": candidate_label(cands[0]) if len(cands) > 0 else "",
                "Aday 2": candidate_label(cands[1]) if len(cands) > 1 else "",
                "Aday 3": candidate_label(cands[2]) if len(cands) > 2 else "",
                "SEÇİLEN uevcb_id": "",
                "Not": "",
            })

    return rows


def main():
    conn = sqlite3.connect(DB_PATH)
    client = get_client()

    print("organization-list çekiliyor...")
    org_cache = client.organization_list(start_date="2020-01-01", end_date="2026-12-31")
    print(f"  ✓ {len(org_cache)} organizasyon")

    print("uevcb-list-bulk çekiliyor (300'lük parçalar)...")
    strict_catalog, relaxed_catalog = build_uevcb_catalog(client, org_cache)

    rows = collect_review_rows(conn, strict_catalog, relaxed_catalog)
    conn.close()

    df = pd.DataFrame(rows, columns=[
        "Şirket", "Bizim santral adımız", "Bizim kurulu gücümüz (MW)", "Durum",
        "Aday 1", "Aday 2", "Aday 3", "SEÇİLEN uevcb_id", "Not",
    ])
    status_order = {"Belirsiz": 0, "Olası": 1, "Bulunamadı": 2}
    df["_sort"] = df["Durum"].map(status_order)
    df = df.sort_values(["_sort", "Şirket", "Bizim santral adımız"]).drop(columns="_sort")

    df.to_excel(OUT_PATH, index=False, sheet_name="Elle Eşleştirme")

    print(f"\n=== Özet ===")
    print(df["Durum"].value_counts().to_string())
    print(f"\nToplam satır: {len(df)}")
    print(f"Kaydedildi: {OUT_PATH}")


if __name__ == "__main__":
    main()
