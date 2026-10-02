"""
Sorgu API'si (Flask)
======================
Dashboard bu API'yi çağırır; EPİAŞ'a hiç dokunmaz — sadece yerel SQLite'ı okur.
Kimlik bilgisi burada YOK, dolayısıyla dashboard'u kullanan kişiler EPİAŞ'a asla
doğrudan erişemez.

Varsayılan davranış: tarih verilmezse "dün" döner (kullanıcı sabah girdiğinde
otomatik olarak dünün verisini görür).

Çalıştırma:
    pip install flask flask-cors
    python api/app.py
    # -> http://localhost:5000

Uç noktalar:
    GET /api/companies                          -> şirket listesi + özet
    GET /api/companies/<code>                    -> tek şirket detayı + santralleri
    GET /api/production?start=&end=&plant_id=     -> santral bazlı günlük üretim + ağırlıklı PTF
    GET /api/production?start=&end=&company=      -> şirket toplamı (tüm santraller)
    GET /api/turkey/overview                       -> Türkiye geneli çeyreklik üretim/tüketim/PTF
"""

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "energy.db"

app = Flask(__name__)
CORS(app)


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def default_dates():
    """Tarih verilmezse: dün-dün (tek gün, en son tamamlanmış gün)."""
    dun = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    return dun, dun


@app.route("/")
def index():
    return send_from_directory(BASE_DIR, "dashboard.html")


@app.route("/api/companies")
def companies():
    conn = db()
    rows = conn.execute(
        """
        SELECT code, name, installed_capacity_mw, yekdem_ratio_pct,
               ges_mw, res_mw, hes_mw, bes_mw, jes_mw, dogalgaz_mw, termik_mw,
               ebitda_mtl, net_debt_mtl,
               (SELECT COUNT(*) FROM plants p WHERE p.company_code = companies.code AND p.is_active=1) AS santral_sayisi
        FROM companies
        ORDER BY installed_capacity_mw DESC
        """
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/companies/<code>")
def company_detail(code):
    conn = db()
    company = conn.execute("SELECT * FROM companies WHERE code=?", (code,)).fetchone()
    if not company:
        return jsonify({"error": "şirket bulunamadı"}), 404

    plants = conn.execute(
        """
        SELECT id, name, installed_capacity_mw, eic_code, sale_type, tariff_usd_cents,
               contract_end_year, plant_type, has_production_data, id_resolved
        FROM plants WHERE company_code=? AND is_active=1 ORDER BY installed_capacity_mw DESC
        """,
        (code,),
    ).fetchall()
    conn.close()

    return jsonify({
        "company": dict(company),
        "plants": [dict(p) for p in plants],
    })


@app.route("/api/plants/<int:plant_id>")
def plant_detail(plant_id):
    conn = db()
    plant = conn.execute("SELECT * FROM plants WHERE id=?", (plant_id,)).fetchone()
    if not plant:
        conn.close()
        return jsonify({"error": "santral bulunamadı"}), 404
    company = conn.execute(
        "SELECT code, name, yekdem_ratio_pct FROM companies WHERE code=?",
        (plant["company_code"],),
    ).fetchone()
    conn.close()
    return jsonify({
        "plant": dict(plant),
        "company": dict(company) if company else None,
    })


@app.route("/api/production")
def production():
    """
    Sorgu parametreleri:
      start, end   : YYYY-MM-DD (verilmezse dün-dün)
      plant_id     : tek santral (opsiyonel)
      company      : şirket kodu, tüm santralleri toplar (opsiyonel)
    plant_id ve company birlikte verilmezse ve hiçbiri yoksa -> TÜM sistem toplamı döner.
    """
    start, end = default_dates()
    start = request.args.get("start", start)
    end = request.args.get("end", end)
    plant_id = request.args.get("plant_id")
    company = request.args.get("company")

    conn = db()

    if plant_id:
        rows = conn.execute(
            """
            SELECT gun, plant_id, plant_name, company_code,
                   toplam_uretim_mwh, ptf_agirlikli_ort_try, ptf_agirlikli_ort_usd, tahmini_gelir_usd
            FROM v_plant_daily
            WHERE plant_id = ? AND gun BETWEEN ? AND ?
            ORDER BY gun
            """,
            (plant_id, start, end),
        ).fetchall()
    elif company:
        rows = conn.execute(
            """
            SELECT gun, company_code, toplam_uretim_mwh, ptf_agirlikli_ort_try, tahmini_gelir_usd
            FROM v_company_daily
            WHERE company_code = ? AND gun BETWEEN ? AND ?
            ORDER BY gun
            """,
            (company, start, end),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT gun,
                   SUM(toplam_uretim_mwh) AS toplam_uretim_mwh,
                   ROUND(SUM(toplam_uretim_mwh * ptf_agirlikli_ort_try) / NULLIF(SUM(toplam_uretim_mwh),0), 2) AS ptf_agirlikli_ort_try,
                   ROUND(SUM(tahmini_gelir_usd), 2) AS tahmini_gelir_usd
            FROM v_company_daily
            WHERE gun BETWEEN ? AND ?
            GROUP BY gun ORDER BY gun
            """,
            (start, end),
        ).fetchall()

    conn.close()
    return jsonify({
        "start": start, "end": end,
        "plant_id": plant_id, "company": company,
        "data": [dict(r) for r in rows],
    })


@app.route("/api/turkey/overview")
def turkey_overview():
    """Türkiye geneli çeyreklik üretim/tüketim/PTF (Excel + analist raporundan sabit yüklenmiş)."""
    conn = db()
    try:
        rows = conn.execute("SELECT * FROM national_quarterly ORDER BY quarter").fetchall()
        data = [dict(r) for r in rows]
    except sqlite3.OperationalError:
        data = []
    conn.close()
    return jsonify(data)


@app.route("/api/health")
def health():
    conn = db()
    n_plants = conn.execute("SELECT COUNT(*) FROM plants WHERE is_active=1").fetchone()[0]
    n_resolved = conn.execute("SELECT COUNT(*) FROM plants WHERE id_resolved=1").fetchone()[0]
    last_fetch = conn.execute("SELECT MAX(dt_utc3) FROM hourly_production").fetchone()[0]
    conn.close()
    return jsonify({
        "status": "ok",
        "aktif_santral": n_plants,
        "epias_kimligi_cozulmus": n_resolved,
        "son_veri_saati": last_fetch,
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
