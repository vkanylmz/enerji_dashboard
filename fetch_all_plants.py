"""
EPİAŞ Şeffaflık Platformu'ndaki TÜM lisanslı santralleri çeker ve
santraller.xlsx olarak kaydeder. Kimlik bilgileri .env dosyasından okunur.

Çalıştırma:
    python fetch_all_plants.py
"""

import os
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from eptr2 import EPTR2

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

username = os.environ["EPTR_USERNAME"]
password = os.environ["EPTR_PASSWORD"]

print("EPİAŞ'a bağlanılıyor (TGT alınıyor)...")
eptr = EPTR2(username=username, password=password, recycle_tgt=True)

print("pp-list çağrılıyor (tüm lisanslı santraller)...")
result = eptr.call("pp-list")

df = result if isinstance(result, pd.DataFrame) else pd.DataFrame(result)

out_path = BASE_DIR / "santraller.xlsx"
df.to_excel(out_path, index=False)

print(f"\n=== Özet ===")
print(f"Santral sayısı : {len(df)}")
print(f"Kolonlar       : {list(df.columns)}")
print(f"Kaydedildi     : {out_path}")
