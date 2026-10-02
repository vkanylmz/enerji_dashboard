Bu proje Türkiye enerji sektörü için üç katmanlı bir dashboard: Türkiye geneli →
şirket → santral. Önce PROJECT_BRIEF.md'yi oku, tüm bağlam orada.

## Excel hakkında önemli bir nokta

`source-data/Enerji_Sektörü.xlsx` başlangıç noktamız ama **güvenilir tek kaynak
değil** — hatalı ve eksik:

- 3 şirket (ALFAS, CWENE, HUNER) hiç yok — sadece isimleri geçiyor
- 3 şirket (ODAS, SMRTG, AKSUE) kendi sekmesinde santral listesi var ama ana
  rosterde (Sayfa1) yok
- Piyasa Değeri / Firma Değeri / FD-FAVÖK kolonları formül hatası (#NAME?/#REF?) —
  Bloomberg/Datastream eklentisine bağlıydı, eklenti olmadan çözülmüyor

Excel'i zamanla **elle düzeltmeler yaparak** kullanacağız (eksik şirketleri
ekleme, hatalı hücreleri temizleme) — ama nihai/kalıcı veri kaynağı Excel değil,
**EPİAŞ'ın kendisi** olmalı. Excel'i sadece EPİAŞ'ta hiç olmayan şeyler için
(piyasa değeri, FAVÖK, net borç gibi finansal veriler) referans olarak tut.
Excel'de bir tutarsızlık gördüğünde bana sor, sessizce üzerine yazma.

## Veri çekme akışı (EPİAŞ'tan)

`source-data/EPIAS_API_dokumantasyonu.pdf` EPİAŞ'ın resmi API dokümanı — 584
sayfa, tüm endpoint'ler ve alanlar orada tanımlı. Halihazırda doğruladığım akış:

1. **Kimlik doğrulama**: `POST https://giris.epias.com.tr/cas/v1/tickets`
   (form-encoded `username`+`password`) → 2 saat geçerli bir TGT token döner.
   Sonraki her istek `TGT` header'ıyla `https://seffaflik.epias.com.tr/electricity-service`
   köküne atılır. Bu akış `backend/scripts/epias_client.py`'da yazılı.

2. **Santral/organizasyon kimliklerini çözme** (`resolve_plant_ids.py`):
   - `powerplant-list` → TEK ÇAĞRIDA Türkiye'deki TÜM santraller `{eic,id,name}`
     ile gelir. Bizim EIC kodlarımızla eşleştirip `pp_id`'yi buluyoruz (YEKDEM
     santrallerinin üretimini çekmek için gerekli).
   - `organization-list` + `uevcb-list-bulk` → şirket bazlı, PTF ile satan
     santrallerin üretimini çekmek için gereken `uevcb_id`'yi buluyor, AYRICA
     bizim listemizde olmayan (Excel'de unutulmuş) santralleri de ortaya
     çıkarıyor.

3. **Günlük veri çekme** (`daily_fetch.py`, cron ile her gün 09:00, dünü çeker):
   - PTF: `mcp` servisi, saatlik
   - YEKDEM'li santraller: `injection-quantity` (UEVM), `pp_id` ile tek tek
   - PTF'li santraller: `dpp-bulk` (KGÜP), `uevcb_id` ile şirket bazlı toplu

Bunların hiçbiri gerçek kimlik bilgileriyle test edilmedi (Claude.ai'nin ağı
EPİAŞ'a kapalıydı) — ilk iş bunu senin ortamında `.env`'e gerçek bilgileri girip
doğrulamak.

## Veritabanı — genişletmemiz gerekecek

`backend/scripts/schema.sql`'de başlangıç şeması var (companies, plants,
hourly_price, hourly_production, national_quarterly) ve SQLite kullanıyor.
**Ama çok daha fazla veri çekeceğim** — bu yüzden şemayı gözden geçirip
production-grade hale getirmeni istiyorum:

- Şu an sadece saatlik üretim + fiyat var. İleride KGÜP/UEVM dışında EAK (emre
  amade kapasite), dengesizlik miktarı/maliyeti, YEKDEM belge/gelir verileri,
  ikili anlaşma hacimleri gibi başka EPİAŞ servislerini de çekeceğim — şemayı
  bu tür yeni tabloların kolayca eklenebileceği, tutarlı bir konvansiyonla
  (örn. her tablo `plant_id` + `dt_utc3` birincil anahtarıyla) tasarla.
- Veri hacmi büyüyecek (yüzlerce santral × saatlik × çok yıllık). SQLite bu
  ölçekte hâlâ makul mü yoksa PostgreSQL'e geçmemiz gerekir mi, karar senin —
  ama gerekçeni açıkla.
- İndeksleme, sayfalama, eski veriyi arşivleme gibi konularda da öneride bulun.

Önce mevcut şemayı ve script'leri incele, eksik şirketleri Excel'den/EPİAŞ'tan
tamamla, canlı kimlik bilgileriyle veri çekme akışını doğrula, sonra veritabanı
mimarisini birlikte gözden geçirelim.
