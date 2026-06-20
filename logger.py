import requests
import psycopg2
import os
import time
from datetime import datetime

# دریافت آدرس دیتابیس از متغیر محیطی Railway
DATABASE_URL = os.environ.get('DATABASE_URL')

# اتصال به PostgreSQL
conn = psycopg2.connect(DATABASE_URL)
cursor = conn.cursor()

# ساخت جدول برای ذخیره آمار (اگر وجود نداشت)
cursor.execute("""
    CREATE TABLE IF NOT EXISTS miner_stats (
        id SERIAL PRIMARY KEY,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        hashrate_total BIGINT,
        hashrate_highest BIGINT,
        shares_good INT,
        shares_total INT,
        pool_url TEXT
    );
""")
conn.commit()

print("✅ متصل به دیتابیس شد و جدول آماده است.")

while True:
    try:
        # خواندن آمار از API ماینر (که روی پورت 8080 اجرا می‌شود)
        response = requests.get('http://localhost:8080/api/summary', timeout=5)
        data = response.json()

        # استخراج فیلدهای مورد نیاز
        hashrate_total = data.get('hashrate', {}).get('total', [0])[0]
        hashrate_highest = data.get('hashrate', {}).get('highest', [0])[0]
        shares_good = data.get('results', {}).get('shares_good', 0)
        shares_total = data.get('results', {}).get('shares_total', 0)
        pool_url = data.get('pool', '')

        # درج اطلاعات در دیتابیس
        cursor.execute("""
            INSERT INTO miner_stats (hashrate_total, hashrate_highest, shares_good, shares_total, pool_url)
            VALUES (%s, %s, %s, %s, %s);
        """, (hashrate_total, hashrate_highest, shares_good, shares_total, pool_url))
        
        conn.commit()
        print(f"[{datetime.now()}] ✅ ذخیره شد: هش {hashrate_total} | شار خوب {shares_good}")

    except Exception as e:
        print(f"[{datetime.now()}] ❌ خطا: {e}")
    
    # هر ۶۰ ثانیه یک بار (۱ دقیقه) اجرا کن
    time.sleep(60)
