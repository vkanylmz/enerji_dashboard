# Türkiye Enerji Sektörü Dashboard — Proje Devir Dokümanı

Bu doküman, bu projeyi Claude.ai sohbetinden Claude Code'a taşırken tüm bağlamı
aktarmak için hazırlandı. Claude Code buradan devam edebilir.

## Hedef

Kullanıcı üç katmanlı bir dashboard istiyor:

1. **Türkiye geneli** (giriş ekranı): toplam kurulu güç, elektrik üretimi/tüketimi,
   ortalama PTF — grafiklerle
2. **Şirket bazlı**: yenilenebilir enerji şirketleri listesi, birine tıklayınca o
   şirketin santralleri
3. **Santral detayı**: kurulu güç, kapasite kullanım oranı, YEKDEM/PTF kapsamında
   satış, günlük/aylık/çeyreklik üretim, üretimden elde edilen gelir

Veri **günlük olarak otomatik güncellenecek** (cron, sabah 09:00, dünün verisini
çeker) ve kullanıcı tarih aralığı + santral seçerek esnek sorgu yapabilecek.
**Kullanıcı hiçbir zaman EPİAŞ'a doğrudan erişmeyecek** — sadece arka plan scripti
(sunucuda, kullanıcının kontrolünde) EPİAŞ'a bağlanır.

## Kaynak veriler (`source-data/`)

- **`Enerji_Sektörü.xlsx`** — kullanıcının kendi hazırladığı çalışma dosyası.
  23 şirketin santral listesi (EIC kodu, kurulu güç, satış tipi PTF/YEKDEM,
  YEKDEM tarifesi), 18 aylık (Oca25-Haz26) santral bazlı üretim ve hesaplanan gelir.
- **`Enerji_Sektoru_2Q26.pdf`** — kullanıcının bu Excel'den ürettiği, 17.07.2026
  tarihli analist raporu (Colendi Menkul Değerler). Excel'deki kırık formüllerin
  (bkz. aşağı) **doğru/çözümlenmiş halini** içeriyor — finansal tablo, kapasite
  kırılımı (GES/RES/HES/vb), YEKDEM oranı, yatırım projeksiyonları.
- **`EPIAS_API_dokumantasyonu.pdf`** — EPİAŞ Şeffaflık Platformu'nun resmi 584
  sayfalık REST API dokümantasyonu (v1.15.15). Kimlik doğrulama akışı ve tüm
  endpoint/DTO şemaları buradan doğrulandı (aşağıda özetli).

### ⚠️ Excel'in bilinen sorunları

| Sorun | Şirketler | Detay |
|---|---|---|
| Hiç veri yok | `ALFAS`, `CWENE`, `HUNER` | Sadece "ENERJİ SEKTÖRÜ" ana tablosunda isim geçiyor |
| Kendi sekmesinde santral listesi var ama "Sayfa1" ana rosterinde YOK | `ODAS`, `SMRTG`, `AKSUE` | `init_db.py`'ın `load_plant_roster()` fonksiyonu sadece Sayfa1'i okuyor, bu 3 şirketi kaçırıyor — DÜZELTİLMELİ |
| Formülleri kırık (`#NAME?`/`#REF!`) | Tüm 23 şirket | Piyasa Değeri, Firma Değeri, FD/FAVÖK — muhtemelen Bloomberg/Datastream eklentisine bağlıydı. PDF raporundaki doğru değerlerle `init_db.py`'da elle dolduruldu (`load_report_financials()`) |

**Önerilen çözüm:** Excel'i yamamak yerine, santral/şirket rosterını **doğrudan
EPİAŞ'tan** kurun — `powerplant-list` + `organization-list` + `uevcb-list-bulk` +
`licensed-powerplant-investment-list` servisleri Türkiye'deki TÜM lisanslı
santralleri kurulu güçleriyle veriyor. Excel/PDF'i sadece EPİAŞ'ta olmayan
finansal veriler (piyasa değeri, FAVÖK, net borç) için referans olarak kullanın.

## Doğrulanmış EPİAŞ API bilgisi

**Kimlik doğrulama** (dokümanın "2. İsteklere Zorunlu Güvenlik Bilgisinin Eklenmesi"):
```
POST https://giris.epias.com.tr/cas/v1/tickets
Content-Type: application/x-www-form-urlencoded
Body: username=...&password=...
-> HTTP 201, gövde = "TGT-..." (2 saat geçerli)
```
Sonraki istekler: `https://seffaflik.epias.com.tr/electricity-service<path>`,
header `TGT: <deger>`.

**Kullanılan endpoint'ler** (`backend/scripts/epias_client.py` içinde uygulandı):

| Servis | Yol | Gövde | Kullanım |
|---|---|---|---|
| PTF | `POST /v1/markets/dam/data/mcp` | `{startDate,endDate}` | Saatlik piyasa fiyatı |
| Organizasyon Listesi | `POST /v1/generation/data/organization-list` | `{startDate,endDate}` | Şirket adı → `organizationId` eşleştirme |
| Santral Listesi (TÜM Türkiye) | `GET /v1/generation/data/powerplant-list` | — | `{eic,id,name}` — EIC→`pp_id`, TEK ÇAĞRIDA |
| Toplu UEVÇB Listesi | `POST /v1/generation/data/uevcb-list-bulk` | `{organizationIds:[...]}` | `{eic,id,name,orgId}` — EIC→`uevcb_id`, org bazlı |
| UEVM (YEKDEM üretimi) | `POST /v1/generation/data/injection-quantity` | `{startDate,endDate,powerplantId}` | Saatlik üretim, `pp_id` ile, tek tek |
| KGÜP toplu (PTF üretimi) | `POST /v1/generation/data/dpp-bulk` | `{date,region:'TR1',uevcbIds:[...]}` | Saatlik üretim, `uevcb_id` ile, şirket bazlı toplu |
| Lisanslı Santral Yatırımları | `POST /v1/generation/data/licensed-powerplant-investment-list` | — | Henüz kullanılmadı — eksik şirketleri (ALFAS/CWENE/HUNER) bulmak için denenebilir |

**Önemli iş kuralı:** YEKDEM'li santraller UEVM ile, PTF'li santraller KGÜP ile
çekiliyor (`plants.sale_type` alanına göre ayrım yapılıyor).

### ⚠️ `eptr2` kütüphanesi kullanılırsa: çağrı adı tuzağı

Projede iki farklı EPİAŞ erişim yolu var:
1. **`epias_client.py`** (bizim kendi ham `requests` istemcimiz) — yukarıdaki
   tablodaki path'leri doğrudan kullanır. `powerplant_list()` metodu
   `GET /v1/generation/data/powerplant-list` çağırır.
2. **`eptr2` paketi** (örn. tek seferlik `fetch_all_plants.py` script'inde) —
   kendi çağrı isim sözlüğünü kullanır, REST path'leriyle **birebir aynı isim
   değil**. Santral listesi için:
   - ✅ **`pp-list`** — "Santral Listeleme", **tarih parametresi istemez**,
     tek çağrıda TÜM Türkiye'yi döner (`epias_client.powerplant_list()` ile
     aynı veriyi verir — 15.09.2026'da 1830 santral, doğrulandı).
   - ❌ `dpp-pp-list` diye bir çağrı **YOK** — `eptr.get_available_calls()`
     listesinde bulunmuyor, kullanılırsa `Exception: This call ... is not yet
     defined` hatası verir. Kaynağı muhtemelen kafada karışan `dpp-bulk` (KGÜP)
     ile `pp-list` (santral listesi) isimleri.
   - `lic-pp-list` — "Lisanslı Santral Yatırımları" (`start_date`/`end_date`
     ister) — REST tablosundaki `licensed-powerplant-investment-list` ile
     eşleşir, farklı bir amaç (ALFAS/CWENE/HUNER gibi eksik şirketleri bulmak
     için).

   `eptr2` ile hangi çağrının doğru isim/parametre aldığından emin değilsen
   önce `eptr.get_available_calls()` ile listeye bak, tahmin etme.

### ✓ Gerçek kimlik bilgisiyle doğrulanan akış (15.09.2026)

- `giris.epias.com.tr` ve `seffaflik.epias.com.tr`'a ağ erişimi sorunsuz, IP
  whitelist engeli yaşanmadı.
- `epias_client.EpiasClient().powerplant_list()` → 1830 santral, `{eic,id,name,shortName}`.
- `resolve_plant_ids.py --step1`: DB'deki 221 santralin 129'unda EIC kodu
  vardı; bu 129'un **tamamı** (129/129) EPİAŞ `pp_id`'siyle eşleşti — eşleşme
  hatası/format sorunu yok. Kalan 92 santralde EIC kodu Excel'de baştan hiç
  yok (şirket sekmelerinde doldurulmamış) — bu bir eşleşme başarısızlığı değil,
  kaynak veri eksikliği; 18 farklı şirkete dağılmış durumda (A1YEN 12, BIGEN 11,
  ECOGR 10, AKFYE 9, ... tek bir şirkette yoğunlaşmıyor), hem PTF hem YEKDEM
  satış tipli santralleri kapsıyor.

**Santral bazlı PTF ağırlıklı ortalama fiyat:**
```
Σ(saatlik_üretim_MWh × o_saatin_PTF_fiyatı) / Σ(saatlik_üretim_MWh)
```
Bu, `schema.sql`'deki `v_plant_daily` view'ında SQL ile hesaplanıyor ve
doğrulandı (bkz. aşağı).

## Mevcut kod durumu (`backend/`)

```
backend/
├── scripts/
│   ├── schema.sql              ✓ Çalışıyor (companies, plants, hourly_price,
│   │                              hourly_production, national_quarterly, view'lar)
│   ├── init_db.py              ✓ ÇALIŞTIRILDI ve doğrulandı — Excel'den 23 şirket,
│   │                              221 santral, 129 EIC kodlu yüklendi. YUKARIDAKİ
│   │                              3 EKSİK ŞİRKETİ EKLEMEK İÇİN GÜNCELLENMELİ.
│   ├── epias_client.py         ⚠ Yazıldı, resmi dokümana dayanıyor, ama GERÇEK
│   │                              kimlik bilgisiyle TEST EDİLMEDİ (bu ortamın ağı
│   │                              EPİAŞ'a kapalı). İLK İŞ BUNU TEST ETMEK.
│   ├── resolve_plant_ids.py    ⚠ Aynı durum — --step1 (EIC→pp_id, tüm Türkiye
│   │                              tek çağrı) ve --step2 (org→uevcb, şirket bazlı +
│   │                              eksik santral bulma) test edilmeli.
│   └── daily_fetch.py          ⚠ Aynı durum — PTF+UEVM+KGÜP günlük çekme.
├── api/
│   └── app.py                  ✓ ÇALIŞTI ve test edildi (demo veriyle). Flask,
│                                   SADECE SQLite okur, EPİAŞ'a hiç dokunmaz.
│                                   Uç noktalar: /api/companies, /api/companies/<code>,
│                                   /api/production (start/end/plant_id/company),
│                                   /api/turkey/overview, /api/health
├── data/
│   └── energy.db                ✓ Excel'den dolduruldu + demo saatlik veri (3
│                                    santral, 1 gün) — gerçek veriyle DEĞİŞTİRİLMELİ
├── dashboard_v1_ornek.html      İlk (basit, tek-günlük) versiyon — üç katmanlı
│                                  arayüz için YENİDEN YAZILMALI, yeni API'yi
│                                  kullanmalı (aşağıya bakın)
└── requirements.txt              requests, flask, flask-cors, openpyxl, pandas
```

## Sırada ne var (öncelik sırasıyla)

1. **`.env` oluşturup gerçek EPİAŞ kimlik bilgilerini gir**, `epias_client.py`'ın
   TGT alıp alamadığını tek bir çağrıyla doğrula (örn. `client.ptf(...)`)
2. **`init_db.py`'ı düzelt**: `ODAS`, `SMRTG`, `AKSUE` sekmelerini de oku (Sayfa1'de
   olmasalar da kendi sekmelerinde santral listesi var); `ALFAS`/`CWENE`/`HUNER`
   için `licensed-powerplant-investment-list` veya organizasyon adı aramasıyla
   EPİAŞ'tan santral listesi bulmayı dene
3. **`resolve_plant_ids.py --step1`** çalıştır, kaç santralin EIC koduyla
   eşleştiğini gör
4. **`resolve_plant_ids.py --step2 --company AKFYE`** ile TEK şirkette dene
   (organizasyon isim eşleştirmesi ilk seferde tam olmayabilir — gerekirse
   `find_org_id()`'deki fuzzy-match mantığını iyileştir), sonra `--all`
5. **`daily_fetch.py --backfill-days 30`** ile geçmişi doldur
6. **Üç katmanlı dashboard arayüzünü** (`dashboard_v1_ornek.html`'i temel alarak
   ama `/api/companies`, `/api/companies/<code>`, `/api/production` uçlarını
   kullanan) yeniden inşa et — Türkiye özeti → şirket kartları (tıklanabilir) →
   santral detay sayfası (tıklanınca kurulu güç, kapasite kullanım oranı,
   YEKDEM/PTF, günlük/aylık/çeyreklik üretim, gelir)
7. Cron'a ekle: her gün 09:00'da `daily_fetch.py`

## Tasarım kararları (neden böyle yapıldı)

- **SQLite + Flask API**, statik JSON değil — çünkü kullanıcı tarih aralığı +
  santral seçerek esnek sorgu yapacak, bu önceden hazırlanmış tek bir dosyayla
  yapılamaz.
- **Kullanıcı EPİAŞ'a hiç dokunmuyor** — sadece `daily_fetch.py` (cron, sunucuda)
  bağlanıyor, `.env`'deki kimlik bilgisiyle. `api/app.py`'da kimlik bilgisi YOK.
- **`eptr2` paketi yerine ham `requests`** — kullanıcı EPİAŞ'ın resmi API
  dokümanını verdikten sonra, üçüncü parti paketin alias isimlerine güvenmek
  yerine doğrulanmış endpoint'leri doğrudan kullanmak daha güvenilir bulundu.
