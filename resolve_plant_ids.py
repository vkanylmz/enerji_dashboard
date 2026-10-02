"""
Santral Kimliklerini Çözme + Eksik Santral Bulma (v2 - doğrulanmış API şemasıyla)
=====================================================================================
EPİAŞ'ın resmi API dokümantasyonundan (transparency-electricity) doğrulanmış
endpoint'leri kullanır — artık tahmin değil.

Adım 1 — powerplant-list (TEK ÇAĞRI, tüm Türkiye):
    GET /v1/generation/data/powerplant-list -> {eic, id, name}
    Excel'deki EIC kodlarıyla eşleştirip epias_pp_id'yi dolduruyoruz.
    (Bu, UEVM/injection-quantity sorgusunda kullanılacak kimlik.)

Adım 2 — uevcb kataloğu (İSİM bazlı eşleştirme, tüm ülke):
    ÖNEMLİ (15.09.2026'da gerçek veriyle doğrulandı, ilk tasarım YANLIŞTI):
    EPİAŞ'ta "şirket bazlı tek organizasyon" yok — SANTRAL BAŞINA AYRI
    ORGANİZASYON (SPV) var (1902 organizasyon ≈ 1830 santral sayısına yakın).
    Şirket markasını (AKFYE gibi) organizasyon adında aramak bu yüzden işe
    yaramıyor. Ayrıca UEVÇB'nin kendi EIC kodu, santralin EIC kodundan
    FARKLI (örn. Sırma HES: santral EIC'i Excel'de 40W000000000692P, UEVÇB
    kataloğunda 40W000003210490U) — EIC üzerinden de eşleştirilemiyor.

    Bunun yerine: organization-list ile TÜM organizasyonlar, sonra
    uevcb-list-bulk ile (300'lük parçalar halinde — tek seferde ~1900 id
    göndermek WAF'a takılıyor, HTTP 403) TÜM UEVÇB'ler çekilip tek bir
    kataloğa toplanır. Bizim santral adlarımız normalize edilip (Türkçe
    karakter sadeleştirme, parantez/noktalama temizleme) bu kataloğa karşı
    eşleştirilir:
      - Birebir normalize isim eşleşmesi, TEK aday       -> güvenli, DB'ye yazılır
      - Birebir eşleşme ama BİRDEN FAZLA aday (bir santral
        birden fazla UEVÇB'ye bölünmüş olabilir)          -> yazılmaz, raporlanır
      - Yakın (fuzzy, difflib) eşleşme                     -> yazılmaz, "kontrol et" diye raporlanır
      - Hiç eşleşme yok                                     -> raporlanır
    (Bu adımdaki uevcb id, KGÜP/dpp-bulk sorgusunda kullanılacak kimlik.)

Kullanım:
    python scripts/resolve_plant_ids.py --step1        # EIC->pp_id eşleştirme (hızlı, tüm şirketler)
    python scripts/resolve_plant_ids.py --step2 --company AKFYE
    python scripts/resolve_plant_ids.py --step2 --all    # dikkat: 23 şirket, biraz sürer
"""

import argparse
import csv
import difflib
import os
import re
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "energy.db"
sys.path.insert(0, str(Path(__file__).resolve().parent))


def load_dotenv_manual():
    env_path = BASE_DIR / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def get_client():
    from epias_client import EpiasClient
    load_dotenv_manual()
    return EpiasClient()


def normalize_eic(eic):
    return (eic or "").strip().upper()


def step1_resolve_pp_ids(client, conn):
    """Tüm Türkiye santral listesini çekip EIC kodu ile eşleştirir."""
    print("powerplant-list çekiliyor (tüm Türkiye, tek çağrı)...")
    plants = client.powerplant_list()
    print(f"  ✓ EPİAŞ'ta toplam {len(plants)} santral bulundu")

    eic_to_pp = {normalize_eic(p.get("eic")): p.get("id") for p in plants if p.get("eic")}

    cur = conn.cursor()
    ours = cur.execute(
        "SELECT id, eic_code FROM plants WHERE eic_code IS NOT NULL AND epias_pp_id IS NULL"
    ).fetchall()

    matched = 0
    for plant_id, eic in ours:
        pp_id = eic_to_pp.get(normalize_eic(eic))
        if pp_id:
            cur.execute(
                "UPDATE plants SET epias_pp_id=?, id_resolved=1 WHERE id=?",
                (pp_id, plant_id),
            )
            matched += 1

    conn.commit()
    print(f"  ✓ {matched} / {len(ours)} santral, EIC kodu üzerinden EPİAŞ pp_id ile eşleşti")
    unmatched = len(ours) - matched
    if unmatched:
        print(f"  ⚠ {unmatched} santralin EIC kodu EPİAŞ listesinde bulunamadı "
              f"(EIC kodu hatalı/eski olabilir — manuel kontrol gerekebilir)")


def step1b_resolve_yekdem_pp_ids(client, conn):
    """YEKDEM'li santraller için DOĞRU pp_id kaynağını kullanır.

    ÖNEMLİ (15.09.2026, gerçek veriyle doğrulandı): powerplant-list'in 'id'si
    ile injection-quantity çağrıldığında 60/63 santralde '(BUS)SEF1122
    Verilen Santral id sistemde bulunamadı' hatası alındı. Kök neden:
    powerplant-list ve licensed-powerplant-list (YEKDEM'e özel) AYNI EIC için
    FARKLI id döndürüyor (örn. Umutlu HES: powerplant-list id=2312,
    licensed-powerplant-list powerPlantId=9862 — ikincisi doğru olan).
    Bu adım step1_resolve_pp_ids()'in yazdığı YEKDEM pp_id'lerini bu doğru
    kaynakla ÜZERİNE YAZAR (step1'i çalıştırmak zorunlu değil ama önerilir)."""
    print("licensed-powerplant-list çekiliyor (YEKDEM'e özel santral listesi)...")
    plants = client.renewable_powerplant_list(period=datetime.now().strftime("%Y-%m-%d"))
    print(f"  ✓ EPİAŞ'ta YEKDEM kapsamında {len(plants)} santral bulundu")

    eic_to_pp = {normalize_eic(p.get("eic")): p.get("powerPlantId") for p in plants if p.get("eic")}

    cur = conn.cursor()
    ours = cur.execute(
        "SELECT id, eic_code, epias_pp_id FROM plants WHERE sale_type='YEKDEM' AND eic_code IS NOT NULL"
    ).fetchall()

    matched, changed = 0, 0
    for plant_id, eic, old_pp_id in ours:
        pp_id = eic_to_pp.get(normalize_eic(eic))
        if pp_id:
            if pp_id != old_pp_id:
                changed += 1
            cur.execute(
                "UPDATE plants SET epias_pp_id=?, id_resolved=1 WHERE id=?",
                (pp_id, plant_id),
            )
            matched += 1

    conn.commit()
    print(f"  ✓ {matched} / {len(ours)} YEKDEM santrali doğru pp_id ile eşleşti "
          f"({changed} tanesinin id'si önceki (yanlış) değerden değiştirildi)")
    unmatched = len(ours) - matched
    if unmatched:
        print(f"  ⚠ {unmatched} YEKDEM santrali licensed-powerplant-list'te bulunamadı")


_TR_TRANSLATE = str.maketrans({
    "İ": "I", "I": "I", "ı": "I",
    "Ç": "C", "ç": "C",
    "Ğ": "G", "ğ": "G",
    "Ü": "U", "ü": "U",
    "Ş": "S", "ş": "S",
    "Ö": "O", "ö": "O",
})


def normalize_plant_name(name):
    """Türkçe karakterleri sadeleştirir, parantez/noktalama temizler.
    'Otluca HES(ELEN)' ve 'OTLUCA HES' aynı anahtara düşsün diye."""
    if not name:
        return ""
    s = str(name).translate(_TR_TRANSLATE).upper()
    s = re.sub(r"\([^)]*\)", " ", s)
    s = re.sub(r"[^A-Z0-9]+", " ", s)
    return " ".join(s.split())


_DROP_TOKENS = {"HIBRIT", "HYBRID", "I", "II", "III", "IV", "V", "1", "2", "3", "4", "5"}
PLANT_TYPE_TOKENS = {"HES", "RES", "GES", "JES", "BES", "DGKCS", "KCS"}


def relax_plant_name(norm_name):
    """Faz/tesis eki farklarını tolere eden ikinci seviye anahtar — 'Hibrit',
    Roma rakamları ve faz numaralarını atar: 'CAMLICA III HES' ve
    'CAMLICA HES' aynı esnetilmiş anahtara düşer."""
    tokens = [t for t in norm_name.split() if t not in _DROP_TOKENS]
    return " ".join(tokens)


def extract_type_token(norm_name):
    """Normalize isimdeki son santral tipi token'ını (HES/RES/GES/...) döner."""
    for t in reversed(norm_name.split()):
        if t in PLANT_TYPE_TOKENS:
            return t
    return None


def build_uevcb_catalog(client, org_cache, chunk_size=300):
    """TÜM Türkiye'nin UEVÇB listesini toplar (parça parça — tek seferde tüm
    organizasyon id'lerini göndermek WAF'a takılıyor, HTTP 403). İki katalog
    döner: (strict, relaxed) — strict tam normalize isme, relaxed 'Hibrit'/faz
    numarası atılmış gevşetilmiş isme göre gruplanır. Değerler uevcb id'ye
    göre de-duplike edilir (bir UEVÇB iki farklı chunk'ta tekrar gelmesin)."""
    org_ids = [o["organizationId"] for o in org_cache]
    strict = {}
    seen_ids = set()
    total = 0
    for i in range(0, len(org_ids), chunk_size):
        chunk = org_ids[i:i + chunk_size]
        items = client.uevcb_list_bulk(chunk, start_date="2020-01-01", end_date="2026-12-31")
        for u in items:
            uid = u.get("id")
            if uid in seen_ids:
                continue
            seen_ids.add(uid)
            key = normalize_plant_name(u.get("name"))
            if key:
                strict.setdefault(key, []).append(u)
        total += len(items)
        time.sleep(0.2)

    relaxed = {}
    for key, items in strict.items():
        rkey = relax_plant_name(key)
        for u in items:
            relaxed.setdefault(rkey, [])
            if u not in relaxed[rkey]:
                relaxed[rkey].append(u)

    print(f"  ✓ {total} UEVÇB kaydı toplandı ({len(seen_ids)} benzersiz), "
          f"{len(strict)} tam / {len(relaxed)} esnetilmiş normalize isim")
    return strict, relaxed


def step2_resolve_company(conn, company_code, strict_catalog, relaxed_catalog):
    cur = conn.cursor()
    row = cur.execute("SELECT code FROM companies WHERE code=?", (company_code,)).fetchone()
    if not row:
        print(f"  ✗ {company_code} companies tablosunda yok")
        return {"matched": [], "ambiguous": [], "fuzzy": [], "unmatched": []}

    plants = cur.execute(
        "SELECT id, name FROM plants WHERE company_code=? AND epias_uevcb_id IS NULL",
        (company_code,),
    ).fetchall()

    strict_keys = list(strict_catalog.keys())
    matched, ambiguous, fuzzy, unmatched = [], [], [], []

    def write_match(plant_id, name, u, tag):
        cur.execute(
            "UPDATE plants SET epias_uevcb_id=?, epias_org_id=? WHERE id=?",
            (u.get("id"), u.get("orgId"), plant_id),
        )
        matched.append((name, u.get("name"), tag))

    for plant_id, name in plants:
        key = normalize_plant_name(name)
        our_type = extract_type_token(key)

        # 1) Tam eşleşme
        candidates = strict_catalog.get(key)
        if candidates and len(candidates) == 1:
            write_match(plant_id, name, candidates[0], "tam")
            continue
        if candidates and len(candidates) > 1:
            ambiguous.append((name, [c.get("name") for c in candidates]))
            continue

        # 2) Esnetilmiş eşleşme (Hibrit/faz numarası atılmış) — tip uyuşmalı
        rkey = relax_plant_name(key)
        rcands = relaxed_catalog.get(rkey, [])
        if our_type:
            rcands = [c for c in rcands
                      if extract_type_token(normalize_plant_name(c.get("name"))) in (our_type, None)]
        if len(rcands) == 1:
            write_match(plant_id, name, rcands[0], "esnetilmiş")
            continue
        if len(rcands) > 1:
            ambiguous.append((name, [c.get("name") for c in rcands]))
            continue

        # 3) Yakın (fuzzy) eşleşme — sadece raporlanır, YAZILMAZ. Tip uyuşmayanlar elenir.
        close = difflib.get_close_matches(key, strict_keys, n=3, cutoff=0.82)
        close_named = []
        for c in close:
            cand = strict_catalog[c][0]
            cand_type = extract_type_token(c)
            if our_type and cand_type and cand_type != our_type:
                continue
            close_named.append(cand.get("name"))
        if close_named:
            fuzzy.append((name, close_named))
        else:
            unmatched.append(name)

    conn.commit()

    n_tam = sum(1 for m in matched if m[2] == "tam")
    n_esnek = sum(1 for m in matched if m[2] == "esnetilmiş")
    print(f"  ✓ {len(matched)} / {len(plants)} santral eşleşti (tam: {n_tam}, esnetilmiş/Hibrit-faz: {n_esnek})")
    if ambiguous:
        print(f"  ⚠ {len(ambiguous)} santral BELİRSİZ (birden fazla UEVÇB adayı, elle seçim gerekir):")
        for name, cands in ambiguous:
            print(f"      '{name}' -> {cands}")
    if fuzzy:
        print(f"  ? {len(fuzzy)} santral için yakın eşleşme bulundu (kontrol edip elle onaylayın):")
        for name, cands in fuzzy:
            print(f"      '{name}' -> olası: {cands}")
    if unmatched:
        print(f"  ✗ {len(unmatched)} santral hiç eşleşmedi: {unmatched}")

    return {"company": company_code, "total": len(plants), "matched": matched,
            "ambiguous": ambiguous, "fuzzy": fuzzy, "unmatched": unmatched}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--step1", action="store_true", help="EIC -> pp_id eşleştirme (UEVM için)")
    parser.add_argument("--step2", action="store_true", help="Organizasyon -> UEVÇB eşleştirme + eksik santral bulma (KGÜP için)")
    parser.add_argument("--company", help="--step2 ile: tek şirket kodu")
    parser.add_argument("--all", action="store_true", help="--step2 ile: tüm şirketler")
    parser.add_argument("--db", default=str(DB_PATH))
    args = parser.parse_args()

    if not args.step1 and not args.step2:
        sys.exit("--step1 ve/veya --step2 belirtmelisiniz")

    conn = sqlite3.connect(args.db)
    client = get_client()

    if args.step1:
        step1_resolve_pp_ids(client, conn)
        step1b_resolve_yekdem_pp_ids(client, conn)

    if args.step2:
        if not args.company and not args.all:
            sys.exit("--step2 ile --company KODU veya --all belirtmelisiniz")
        codes = [args.company] if args.company else [r[0] for r in conn.execute("SELECT code FROM companies")]

        print("\norganization-list çekiliyor (tek çağrı, tüm organizasyonlar)...")
        org_cache = client.organization_list(start_date="2020-01-01", end_date="2026-12-31")
        print(f"  ✓ {len(org_cache)} organizasyon bulundu")

        print("uevcb-list-bulk çekiliyor (300'lük parçalar halinde, tüm ülke)...")
        strict_catalog, relaxed_catalog = build_uevcb_catalog(client, org_cache)

        results = []
        for code in codes:
            print(f"\n{code}:")
            results.append(step2_resolve_company(conn, code, strict_catalog, relaxed_catalog))

        total_plants = sum(r["total"] for r in results)
        total_matched = sum(len(r["matched"]) for r in results)
        print(f"\n=== Toplam ({len(codes)} şirket) ===")
        print(f"Eşleşen (uevcb_id kazandı): {total_matched} / {total_plants}")
        need_review = sum(len(r["ambiguous"]) + len(r["fuzzy"]) + len(r["unmatched"]) for r in results)
        print(f"Elle inceleme bekliyor: {need_review}")

        review_path = BASE_DIR / "uevcb_manual_review.csv"
        with open(review_path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["sirket", "santral_adi", "durum", "adaylar"])
            for r in results:
                for name, cands in r["ambiguous"]:
                    w.writerow([r["company"], name, "belirsiz (birden fazla aday)", " | ".join(cands)])
                for name, cands in r["fuzzy"]:
                    w.writerow([r["company"], name, "olasi (yakin eslesme)", " | ".join(cands)])
                for name in r["unmatched"]:
                    w.writerow([r["company"], name, "bulunamadi", ""])
        print(f"📋 İnceleme listesi → {review_path}")

    conn.close()


if __name__ == "__main__":
    main()
