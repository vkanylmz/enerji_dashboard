"""
EPİAŞ Şeffaflık Platformu - Doğrudan API İstemcisi
======================================================
Resmi API dokümantasyonundan (transparency-electricity, v1.15.15) doğrulanmış
endpoint'lerle çalışır. Üçüncü parti pakete (eptr2) ihtiyaç yok, sadece `requests`.

Kimlik doğrulama akışı (dokümanın "2. İsteklere Zorunlu Güvenlik Bilgisinin
Eklenmesi" bölümünden):
    POST https://giris.epias.com.tr/cas/v1/tickets
    Body (form): username=...&password=...
    -> 201 Created, gövde = "TGT-..." (2 saat geçerli)

Sonraki tüm istekler:
    https://seffaflik.epias.com.tr/electricity-service<path>
    Header: TGT: <tgt_degeri>
"""

import os
import time
from datetime import datetime, timedelta

import requests

TGT_URL = "https://giris.epias.com.tr/cas/v1/tickets"
BASE_URL = "https://seffaflik.epias.com.tr/electricity-service"


def _iso(date_str):
    """'YYYY-MM-DD' -> 'YYYY-MM-DDT00:00:00+03:00'.
    EPİAŞ'ın tüm tarih parametreli uçları (mcp, organization-list, dpp-bulk,
    injection-quantity, ...) düz 'YYYY-MM-DD' kabul ETMİYOR, tam ISO 8601 +
    saat dilimi (+03:00) istiyor — aksi halde CORE00003 (400) hatası dönüyor.
    Doğrulandı: 15.09.2026, gerçek kimlik bilgisiyle. Zaten 'T' içeren bir
    değer verilirse olduğu gibi bırakılır (idempotent)."""
    if date_str and "T" not in str(date_str):
        return f"{date_str}T00:00:00+03:00"
    return date_str


class EpiasClient:
    def __init__(self, username=None, password=None):
        self.username = username or os.environ.get("EPTR_USERNAME")
        self.password = password or os.environ.get("EPTR_PASSWORD")
        if not self.username or not self.password:
            raise ValueError(
                "EPTR_USERNAME / EPTR_PASSWORD gerekli (.env dosyasından veya parametre olarak)."
            )
        self._tgt = None
        self._tgt_fetched_at = None

    def _get_tgt(self):
        """TGT 2 saat geçerli; 100 dakikadan eskiyse yeniler."""
        if self._tgt and self._tgt_fetched_at and \
                (datetime.now() - self._tgt_fetched_at) < timedelta(minutes=100):
            return self._tgt

        resp = requests.post(
            TGT_URL,
            data={"username": self.username, "password": self.password},
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "text/plain"},
            timeout=30,
        )
        if resp.status_code != 201:
            raise RuntimeError(
                f"EPİAŞ girişi başarısız (HTTP {resp.status_code}): {resp.text[:300]}\n"
                "Kullanıcı adı/şifrenizi ve seffaflik.epias.com.tr kaydınızı kontrol edin."
            )
        self._tgt = resp.text.strip()
        self._tgt_fetched_at = datetime.now()
        return self._tgt

    def call(self, path, method="POST", body=None, params=None, max_retries=4):
        """
        path: '/v1/generation/data/organization-list' gibi (BASE_URL'e eklenir)
        body: JSON gövde (dict) - POST istekleri için
        params: query string parametreleri - GET istekleri için

        429 (Too Many Requests) alınırsa üstel geri çekilmeyle (Retry-After
        varsa onu, yoksa 2/4/8/16sn) yeniden dener — doğrulandı (15.09.2026):
        saatlik kota (RateLimit-Limit=3600/saat) doldurmadan, sadece kısa
        süreli yoğun istekte (örn. 63 santrali art arda sorgulamak) 429
        geliyor; birkaç saniye beklemek yetiyor."""
        tgt = self._get_tgt()
        headers = {"TGT": tgt, "Content-Type": "application/json", "Accept": "application/json"}
        url = BASE_URL + path

        for attempt in range(max_retries + 1):
            if method.upper() == "GET":
                resp = requests.get(url, headers=headers, params=params, timeout=60)
            else:
                resp = requests.post(url, headers=headers, json=body or {}, timeout=60)

            if resp.status_code == 429 and attempt < max_retries:
                wait = int(resp.headers.get("Retry-After", 2 ** (attempt + 1)))
                time.sleep(wait)
                continue
            break

        if resp.status_code != 200:
            raise RuntimeError(f"EPİAŞ isteği başarısız ({path}, HTTP {resp.status_code}): {resp.text[:300]}")
        return resp.json()

    # ------------------------------------------------------------------
    # Doğrulanmış servis kısayolları (resmi dokümandan)
    # ------------------------------------------------------------------

    def ptf(self, start_date, end_date):
        """Piyasa Takas Fiyatı (PTF) - saatlik. Dönen: items[].{date,hour,price,priceUsd,priceEur}"""
        return self.call(
            "/v1/markets/dam/data/mcp",
            body={"startDate": _iso(start_date), "endDate": _iso(end_date)},
        )["items"]

    def organization_list(self, start_date, end_date):
        """Tüm organizasyonlar. Dönen: items[].{organizationId,organizationName,organizationShortName,organizationEtsoCode}"""
        return self.call(
            "/v1/generation/data/organization-list",
            body={"startDate": _iso(start_date), "endDate": _iso(end_date)},
        )["items"]

    def powerplant_list(self):
        """TÜM Türkiye santralleri (TEİAŞ bazlı). Dönen: items[].{eic,id,name,shortName}
        UEVM (injection-quantity) sorgusunda kullanılan 'powerplantId' buradan gelir.
        Doğrulandı (15.09.2026, gerçek kimlik bilgisiyle): 1830 santral döndü.
        Not: eptr2 kütüphanesindeki karşılığı 'pp-list' (tarih parametresi istemez) —
        'dpp-pp-list' diye bir çağrı YOK, karıştırılmamalı (bkz. PROJECT_BRIEF.md)."""
        return self.call("/v1/generation/data/powerplant-list", method="GET")["items"]

    def renewable_powerplant_list(self, period):
        """YEKDEM kapsamındaki (destek mekanizmasına kayıtlı) santraller.
        Dönen: items[].{powerPlantId,eic,name,shortName}.

        ÖNEMLİ (15.09.2026, gerçek veriyle doğrulandı): Bu uçtaki
        'powerPlantId', powerplant_list()'teki 'id' ile AYNI DEĞİL — aynı
        EIC için bile farklı sayılar dönebiliyor (örn. Umutlu HES:
        powerplant-list id=2312, buradaki powerPlantId=9862). YEKDEM
        santralleri için injection_quantity()'ye VERİLECEK doğru kimlik
        BURADAN gelir; powerplant_list()'in id'siyle çağrılırsa EPİAŞ
        '(BUS)SEF1122 Verilen Santral id sistemde bulunamadı' hatası verir.
        `POST /v1/renewables/data/licensed-powerplant-list` — GET değil, 404
        döner; period JSON gövdede olmalı."""
        return self.call(
            "/v1/renewables/data/licensed-powerplant-list",
            body={"period": _iso(period)},
        )

    def uevcb_list_bulk(self, organization_ids, start_date, end_date):
        """Verilen organizasyon id'lerine ait UEVÇB'ler. Dönen: items[].{eic,id,name,orgId}
        KGÜP (dpp/dpp-bulk) sorgusunda kullanılan 'uevcbId' buradan gelir.

        Doğrulandı (15.09.2026, gerçek kimlik bilgisiyle):
        - `startDate`/`endDate` ZORUNLU (dokümantasyonda/ilk taslakta atlanmıştı) —
          olmadan 'startDate alanı boş olamaz' (VAL SEF1144) hatası döner.
        - `organizationIds` listesi çok büyükse (~1900 id, tüm ülke) 403 WAF
          engeline takılıyor — 300'lük parçalar halinde göndermek güvenli.
        - Buradaki 'eic', santral/powerplant-list'teki 'eic' ile AYNI DEĞİL —
          UEVÇB kendi ayrı EIC koduna sahip (örn. Sırma HES santral EIC'i
          Excel'de 40W000000000692P iken, UEVÇB kataloğunda 40W000003210490U
          olarak kayıtlı). EIC üzerinden değil, İSİM üzerinden eşleştirilmeli
          (bkz. resolve_plant_ids.py step2 — fuzzy isim eşleştirmesi)."""
        return self.call(
            "/v1/generation/data/uevcb-list-bulk",
            body={
                "organizationIds": organization_ids,
                "startDate": _iso(start_date),
                "endDate": _iso(end_date),
            },
        )["items"]

    def injection_quantity(self, start_date, end_date, powerplant_id):
        """UEVM: bir santralin saatlik gerçekleşen üretimi (YEKDEM kapsamındaki santraller için).
        Dönen: items[].{date, ..., total} - 'total' o saatteki üretim (MWh)."""
        return self.call(
            "/v1/generation/data/injection-quantity",
            body={"startDate": _iso(start_date), "endDate": _iso(end_date), "powerplantId": powerplant_id},
        )["items"]

    def kgup_bulk(self, date, uevcb_ids, region="TR1"):
        """KGÜP: birden fazla UEVÇB için tek günün saatlik üretim planı (PTF ile satan santraller).
        Dönen: items[].{time, toplam, uevcbId, uevcbName}."""
        return self.call(
            "/v1/generation/data/dpp-bulk",
            body={"date": _iso(date), "region": region, "uevcbIds": uevcb_ids},
        )["items"]
