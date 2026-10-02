# Enerji Dashboard v2 — Saatlik Üretim + PTF Ağırlıklı Fiyat Mimarisi

> **Güncelleme:** Artık `eptr2` paketine bağımlı değil. EPİAŞ'ın resmi API
> dokümantasyonundan (`transparency-electricity`, v1.15.15 — sizin paylaştığınız
> `index.pdf`) doğrulanmış endpoint'lerle `scripts/epias_client.py` adında
> bağımsız, ham `requests` tabanlı bir istemci yazıldı. Kimlik doğrulama akışı
> (`giris.epias.com.tr/cas/v1/tickets` → TGT → `TGT` header'ı ile istekler) ve
> kullanılan servisler (`organization-list`, `powerplant-list`, `uevcb-list-bulk`,
> `injection-quantity`/UEVM, `dpp-bulk`/KGÜP, `mcp`/PTF) dokümanın 5. ve 6.
> bölümlerinden birebir alındı — artık tahmini alias değil.

Bu sürüm, statik `data.json` yerine gerçek bir **SQLite veritabanı + sorgu API'si**
kullanır. Sebep: artık kullanıcı tarih aralığı ve santral seçerek esnek sorgu yapacak
— bu, önceden hazırlanmış tek bir JSON dosyasıyla yapılamaz.

## Mimari

```
[EPİAŞ Şeffaflık Platformu]
        │  (sadece scripts/daily_fetch.py buraya bağlanır, kimlik bilgisi SADECE .env'de)
        ▼
[scripts/daily_fetch.py]  ← cron ile her gün 09:00'da çalışır, DÜNÜ çeker
        │
        ▼
[data/energy.db]  (SQLite — companies, plants, hourly_price, hourly_production)
        │
        ▼
[api/app.py]  (Flask — sadece bu DB'yi okur, EPİAŞ'a hiç dokunmaz)
        │
        ▼
[dashboard]  (kullanıcı burayı görür — Türkiye geneli → şirket → santral)
```

**Kritik nokta:** Kullanıcı hiçbir zaman EPİAŞ'a dokunmaz. Sadece `daily_fetch.py`
(sunucunuzda, cron ile, sizin kontrolünüzde) EPİAŞ'a bağlanır. Dashboard/API bu bilgiye
sahip bile değil — `api/app.py` içinde kimlik bilgisi yok.

## Dosyalar

```
v2/
├── scripts/
│   ├── schema.sql              ← veritabanı şeması
│   ├── init_db.py              ← Excel'i okuyup DB'yi ilk kez dolduran script (ÇALIŞTIRILDI ✓)
│   ├── daily_fetch.py          ← GÜNLÜK çalışacak script (cron, 09:00) — henüz test edilmedi*
│   └── resolve_plant_ids.py    ← eksik santralleri bulan + ID eşleştiren script — henüz test edilmedi*
├── api/
│   └── app.py                  ← Flask sorgu API'si (ÇALIŞTI ✓, demo veriyle test edildi)
└── data/
    └── energy.db                ← 23 şirket + 221 santral (Excel'den), demo saatlik veri (3 santral, 1 gün)
```

**\*Neden "henüz test edilmedi":** Bu ortamın ağ erişimi EPİAŞ'ın sunucularına kapalı
(sadece pip/npm gibi paket adreslerine izinli), dolayısıyla `daily_fetch.py` ve
`resolve_plant_ids.py`'ı gerçek kimlik bilgilerinizle çalıştırıp doğrulayamadım.
`init_db.py` ve `api/app.py` ise gerçek Excel'iniz ve demo saatlik veriyle uçtan uca
test edildi ve çalışıyor (aşağıda örnek çıktı var). Ancak artık kod tahmini bir
üçüncü-parti pakete değil, sizin verdiğiniz **resmi EPİAŞ API dokümantasyonuna**
dayanıyor — endpoint yolları, gövde alanları ve kimlik doğrulama akışı dokümandan
birebir doğrulandı, dolayısıyla ilk denemede çalışma ihtimali çok daha yüksek.

## resolve_plant_ids.py nasıl çalışıyor (2 adım)

- **`--step1`**: `powerplant-list` servisi TEK ÇAĞRIDA Türkiye'deki tüm santralleri
  `{eic, id, name}` ile döner. Bizim EIC kodlarımızla eşleştirip `epias_pp_id`'yi
  dolduruyoruz — organizasyon/şirket eşleştirmesine bile gerek yok. Bu, YEKDEM'li
  santrallerin UEVM verisini çekmek için yeterli.
- **`--step2`**: PTF ile satan santrallerin üretimi (KGÜP/`dpp-bulk`) **UEVÇB id**
  gerektiriyor, o da organizasyon (şirket) bazlı çekiliyor. Şirket adını EPİAŞ'ın
  organizasyon listesiyle (fuzzy match) eşleştirip o organizasyonun tüm UEVÇB'lerini
  çekiyoruz. Excel'deki EIC listemizde OLMAYAN her UEVÇB = muhtemelen unutulmuş bir
  santral → `missing_plants_report.csv`'ye yazılıyor, onaylayana kadar pasif kalıyor.

## Şu ana kadar doğrulanan (bu ortamda çalıştırıldı)

```bash
python scripts/init_db.py --excel Enerji_Sektörü.xlsx --db data/energy.db
# ✓ 23 şirket, 221 santral, 129 EIC kodlu, Türkiye geneli 6 çeyrek yüklendi

python api/app.py
curl http://localhost:5000/api/production?plant_id=22
# {"data": [{"gun": "2026-09-14", "toplam_uretim_mwh": 76.17,
#            "ptf_agirlikli_ort_try": 2361.51, "ptf_agirlikli_ort_usd": 53.67,
#            "tahmini_gelir_usd": 4088.08}], ...}
```

Yani **hesaplama mantığı doğru çalışıyor** — santral bazlı saatlik üretim × o saatin
PTF'i → ağırlıklı ortalama fiyat ve gelir. Eksik olan tek şey: bu tabloları gerçek
EPİAŞ verisiyle doldurmak (`daily_fetch.py` + `resolve_plant_ids.py`'ı sizin kimlik
bilgilerinizle çalıştırmak).

## Sırada ne var (Claude Code'da yapılmasını önerdiğim kısım)

1. **`.env` oluşturup gerçek EPİAŞ kimlik bilgilerinizi girin** (eskisiyle aynı format:
   `EPTR_USERNAME=...`, `EPTR_PASSWORD=...`)
2. `pip install -r requirements.txt` (artık sadece `requests`, `flask`, `flask-cors`,
   `openpyxl`, `pandas` — `eptr2` gerekmiyor)
3. `python scripts/resolve_plant_ids.py --step1` — EIC→pp_id eşleştirme (tüm şirketler, hızlı)
4. `python scripts/resolve_plant_ids.py --step2 --company AKFYE` ile önce TEK şirkette
   deneyin (organizasyon eşleştirmesi düzgün çalışıyor mu görmek için), sonra `--all`
5. `python scripts/daily_fetch.py --backfill-days 30` — son 30 günü geriye dönük doldurur
6. Cron'a ekleyin: her gün 09:00'da `daily_fetch.py` (dünü çeker)
6. Dashboard arayüzünü (Türkiye geneli → şirket kartları → santral detay sayfası) bu
   API'nin uçlarını (`/api/companies`, `/api/companies/<code>`, `/api/production`)
   kullanacak şekilde inşa edelim — bu, gerçek veriyle görüp anında düzeltebileceğiniz
   bir ortamda (Claude Code) çok daha hızlı ilerler.

## API uçları (özet)

| Uç nokta | Açıklama |
|---|---|
| `GET /api/companies` | Tüm şirketler + kurulu güç, YEKDEM oranı, FAVÖK |
| `GET /api/companies/<code>` | Şirket detayı + tüm santralleri |
| `GET /api/production?start=&end=&plant_id=` | Tek santral, tarih aralığı, günlük üretim + PTF ağırlıklı ort. |
| `GET /api/production?start=&end=&company=` | Şirket toplamı (tüm santraller) |
| `GET /api/production?start=&end=` | Türkiye'deki tüm izlenen santrallerin toplamı |
| `GET /api/turkey/overview` | Çeyreklik üretim/tüketim/PTF (Excel+rapor kaynaklı) |
| `GET /api/health` | Kaç santralin kimliği çözülmüş, son veri hangi saatte |

Tarih verilmezse `start`/`end` otomatik **dün** olur — tam istediğiniz gibi, kullanıcı
sabah girdiğinde dünün verisini görür.
