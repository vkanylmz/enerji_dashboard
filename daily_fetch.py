"""
Günlük Saatlik Üretim + PTF Çekme Scripti (v2 - doğrulanmış API şemasıyla)
==============================================================================
Her gün (cron ile, sabah 09:00 önerilir) çalıştırıldığında dünün verisini çeker:
  1. PTF (saatlik)                              -> hourly_price
  2. YEKDEM'li santraller: UEVM (injection-quantity, epias_pp_id ile, tek tek)
     PTF'li santraller:    KGÜP (dpp-bulk, epias_uevcb_id ile, şirket bazında toplu)
                                                  -> hourly_production

ÖNEMLİ: Bu script SADECE sunucuda/cron'da çalışır ve SADECE .env'deki kimlik
bilgisini kullanır. Dashboard kullanıcıları bu scripti tetikleyemez.

Ön koşul: santrallerin epias_pp_id / epias_uevcb_id alanları dolu olmalı
(bkz. resolve_plant_ids.py --step1 ve --step2).

Kullanım:
    python scripts/daily_fetch.py                     # dünü çeker (cron için)
    python scripts/daily_fetch.py --date 2026-09-10    # belirli bir günü çeker
    python scripts/daily_fetch.py --backfill-days 30   # son 30 günü sırayla çeker
"""

import argparse
import os
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "energy.db"
sys.path.insert(0, str(Path(__file__).resolve().parent))


def load_dotenv_manual():
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        sys.exit(".env dosyası yok. .env.example'ı .env olarak kopyalayıp bilgilerinizi girin.")
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def get_client():
    from epias_client import EpiasClient
    load_dotenv_manual()
    return EpiasClient()


def iso(date_str, hour=0):
    return f"{date_str}T{hour:02d}:00:00+03:00"


def fetch_ptf(client, conn, date_str):
    try:
        items = client.ptf(iso(date_str), iso(date_str, 23))
    except Exception as e:
        print(f"  ✗ PTF çekilemedi: {e}")
        return

    cur = conn.cursor()
    for it in items:
        dt = str(it["date"])[:16].replace("T", " ")
        cur.execute(
            """INSERT INTO hourly_price (dt_utc3, ptf_try, ptf_usd)
               VALUES (?, ?, ?)
               ON CONFLICT(dt_utc3) DO UPDATE SET ptf_try=excluded.ptf_try, ptf_usd=excluded.ptf_usd""",
            (dt, it.get("price"), it.get("priceUsd")),
        )
    conn.commit()
    print(f"  ✓ PTF: {len(items)} saat yazıldı")


def fetch_uevm_plants(client, conn, date_str):
    """YEKDEM'li, pp_id'si bilinen santraller — tek tek (injection-quantity powerplantId ile)."""
    cur = conn.cursor()
    plants = cur.execute(
        """SELECT id, name, company_code, epias_pp_id FROM plants
           WHERE is_active=1 AND sale_type='YEKDEM' AND epias_pp_id IS NOT NULL"""
    ).fetchall()

    ok, failed = 0, 0
    for plant_id, name, company, pp_id in plants:
        try:
            items = client.injection_quantity(iso(date_str), iso(date_str, 23),
                                                powerplant_id=pp_id)
            for it in items:
                dt = str(it.get("date"))[:16].replace("T", " ")
                mwh = it.get("total")
                if mwh is None:
                    continue
                cur.execute(
                    """INSERT INTO hourly_production (plant_id, dt_utc3, production_mwh, data_source)
                       VALUES (?, ?, ?, 'uevm')
                       ON CONFLICT(plant_id, dt_utc3) DO UPDATE SET production_mwh=excluded.production_mwh""",
                    (plant_id, dt, float(mwh)),
                )
            ok += 1
        except Exception as e:
            cur.execute(
                "INSERT INTO fetch_log (target_date, plant_id, status, message) VALUES (?,?,?,?)",
                (date_str, plant_id, "error", f"UEVM {company}/{name}: {e}"),
            )
            failed += 1
        time.sleep(0.15)  # 63 santrali art arda sorgularken EPİAŞ'ın 429 (rate limit) vermesini önler

    conn.commit()
    print(f"  ✓ UEVM (YEKDEM santralleri): {ok} OK, {failed} başarısız")


def fetch_kgup_plants(client, conn, date_str):
    """PTF'li, uevcb_id'si bilinen santraller — şirket bazında toplu (dpp-bulk)."""
    cur = conn.cursor()
    rows = cur.execute(
        """SELECT id, company_code, epias_uevcb_id FROM plants
           WHERE is_active=1 AND sale_type='PTF' AND epias_uevcb_id IS NOT NULL"""
    ).fetchall()

    by_company = defaultdict(list)
    uevcb_to_plant = {}
    for plant_id, company, uevcb_id in rows:
        by_company[company].append(uevcb_id)
        uevcb_to_plant[uevcb_id] = plant_id

    ok, failed = 0, 0
    for company, uevcb_ids in by_company.items():
        try:
            items = client.kgup_bulk(iso(date_str), uevcb_ids)
            for it in items:
                uevcb_id = it.get("uevcbId")
                plant_id = uevcb_to_plant.get(uevcb_id)
                if plant_id is None:
                    continue
                dt_raw = str(it.get("time"))
                dt = f"{date_str} {dt_raw[:5]}" if ":" in dt_raw else dt_raw
                mwh = it.get("toplam")
                if mwh is None:
                    continue
                cur.execute(
                    """INSERT INTO hourly_production (plant_id, dt_utc3, production_mwh, data_source)
                       VALUES (?, ?, ?, 'kgup')
                       ON CONFLICT(plant_id, dt_utc3) DO UPDATE SET production_mwh=excluded.production_mwh""",
                    (plant_id, dt, float(mwh)),
                )
            ok += len(uevcb_ids)
        except Exception as e:
            for uevcb_id in uevcb_ids:
                cur.execute(
                    "INSERT INTO fetch_log (target_date, plant_id, status, message) VALUES (?,?,?,?)",
                    (date_str, uevcb_to_plant.get(uevcb_id), "error", f"KGÜP {company}: {e}"),
                )
            failed += len(uevcb_ids)

    conn.commit()
    print(f"  ✓ KGÜP (PTF santralleri, şirket bazlı toplu): {ok} OK, {failed} başarısız")


def run_for_date(client, conn, date_str):
    print(f"\n📅 {date_str}")
    fetch_ptf(client, conn, date_str)
    fetch_uevm_plants(client, conn, date_str)
    fetch_kgup_plants(client, conn, date_str)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=None, help="YYYY-MM-DD (varsayılan: dün)")
    parser.add_argument("--backfill-days", type=int, default=None)
    parser.add_argument("--db", default=str(DB_PATH))
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    client = get_client()

    if args.backfill_days:
        for i in range(args.backfill_days, 0, -1):
            d = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
            run_for_date(client, conn, d)
    else:
        target = args.date or (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        run_for_date(client, conn, target)

    conn.close()
    print("\n✓ Tamamlandı.")


if __name__ == "__main__":
    main()
