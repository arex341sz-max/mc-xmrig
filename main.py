import asyncio
import json
import os
import time
import aiofiles
import httpx
from datetime import datetime
from pathlib import Path
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

# ─── تنظیمات ──────────────────────────────────────────────────────────────────
app = FastAPI(title="Monero Mining Dashboard")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DATA_DIR = Path("/data")  # در Railway این پوشه persistence دارد
DATA_FILE = DATA_DIR / "miner_stats.json"
MINER_API_URL = "http://localhost:8080/api/summary"  # API ماینر روی همین کانتینر

# ─── وضعیت در حافظه ──────────────────────────────────────────────────────────
state = {
    "hashrate": {"total": 0, "highest": 0},
    "shares": {"good": 0, "total": 0, "accepted": 0, "rejected": 0},
    "traffic": {"sent_mb": 0, "recv_mb": 0},
    "pool": "",
    "uptime": 0,
    "last_update": None,
    "connections": 0,
    "total_hashes": 0,
}
history = []  # حداکثر ۱۰۰ نقطه برای نمودار
SAVE_LOCK = asyncio.Lock()
http_client = None

# ─── بارگذاری از فایل در شروع ────────────────────────────────────────────────
async def load_state():
    global state, history
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if DATA_FILE.exists():
            async with aiofiles.open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.loads(await f.read())
            state.update(data.get("state", {}))
            history.extend(data.get("history", []))
            # محدود کردن history به ۱۰۰ نقطه
            if len(history) > 100:
                history = history[-100:]
    except Exception as e:
        print(f"⚠️ بارگذاری فایل ناموفق: {e}")

async def save_state():
    async with SAVE_LOCK:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            data = {
                "state": state,
                "history": history[-100:],
                "saved_at": datetime.now().isoformat(),
            }
            tmp = DATA_FILE.with_suffix(".tmp")
            async with aiofiles.open(tmp, "w", encoding="utf-8") as f:
                await f.write(json.dumps(data, ensure_ascii=False, indent=2))
            tmp.replace(DATA_FILE)
        except Exception as e:
            print(f"⚠️ ذخیره فایل ناموفق: {e}")

# ─── دریافت آمار از ماینر ────────────────────────────────────────────────────
async def fetch_miner_stats():
    global state, history
    try:
        if http_client is None:
            return
        resp = await http_client.get(MINER_API_URL, timeout=5.0)
        if resp.status_code != 200:
            return
        data = resp.json()
        # استخراج فیلدها بر اساس ساختار XMRig API
        hashrate_total = data.get("hashrate", {}).get("total", [0])[0]
        hashrate_highest = data.get("hashrate", {}).get("highest", [0])[0]
        shares_good = data.get("results", {}).get("shares_good", 0)
        shares_total = data.get("results", {}).get("shares_total", 0)
        pool_url = data.get("pool", "")

        # به‌روزرسانی state
        state["hashrate"]["total"] = hashrate_total
        state["hashrate"]["highest"] = max(state["hashrate"]["highest"], hashrate_highest)
        state["shares"]["good"] = shares_good
        state["shares"]["total"] = shares_total
        state["pool"] = pool_url
        state["last_update"] = datetime.now().isoformat()
        state["uptime"] = int(time.time() - state.get("start_time", time.time()))
        if "start_time" not in state:
            state["start_time"] = time.time()

        # ذخیره نقطه تاریخچه (هر دقیقه یکبار)
        history.append({
            "time": datetime.now().isoformat(),
            "hashrate": hashrate_total,
            "shares": shares_good,
        })
        if len(history) > 100:
            history = history[-100:]

        # ذخیره در فایل
        await save_state()
        print(f"✅ به‌روزرسانی: هش {hashrate_total/1e6:.2f} MH/s | شار {shares_good}")

    except Exception as e:
        print(f"❌ خطا در دریافت آمار: {e}")

# ─── تایمر پس‌زمینه ──────────────────────────────────────────────────────────
async def periodic_fetch():
    while True:
        await fetch_miner_stats()
        await asyncio.sleep(10)  # هر ۱۰ ثانیه

# ─── رویدادهای شروع و پایان ──────────────────────────────────────────────────
@app.on_event("startup")
async def startup():
    global http_client
    http_client = httpx.AsyncClient(timeout=10.0)
    await load_state()
    # اگر state خالی است، start_time را تنظیم کن
    if "start_time" not in state:
        state["start_time"] = time.time()
    asyncio.create_task(periodic_fetch())
    print("🚀 داشبورد ماینینگ راه‌اندازی شد")

@app.on_event("shutdown")
async def shutdown():
    await save_state()
    if http_client:
        await http_client.aclose()

# ─── API برای دریافت آمار ────────────────────────────────────────────────────
@app.get("/api/stats")
async def get_stats():
    return JSONResponse(state)

@app.get("/api/history")
async def get_history():
    return JSONResponse(history[-100:])

# ─── صفحه HTML داشبورد ──────────────────────────────────────────────────────
HTML_PAGE = """
<!DOCTYPE html>
<html lang="fa" dir="rtl">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>داشبورد ماینینگ مونرو</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        *{margin:0;padding:0;box-sizing:border-box}
        body{font-family:'Segoe UI',Tahoma,sans-serif;background:#0a0f1e;color:#e0e8f0;padding:20px;direction:rtl}
        .container{max-width:1200px;margin:auto}
        h1{text-align:center;margin-bottom:25px;color:#4fc3f7}
        .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:18px;margin-bottom:30px}
        .card{background:#141b2b;border-radius:12px;padding:18px;border:1px solid #2a3a5c;box-shadow:0 4px 12px rgba(0,0,0,.4)}
        .card .label{font-size:13px;color:#8899bb;font-weight:600}
        .card .value{font-size:28px;font-weight:700;margin-top:6px;color:#b0d4ff}
        .card .sub{font-size:12px;color:#6a7fa0;margin-top:3px}
        .chart-wrap{background:#141b2b;border-radius:12px;padding:20px;border:1px solid #2a3a5c;margin-bottom:30px}
        .chart-wrap canvas{width:100% !important;height:280px !important}
        .footer{text-align:center;color:#4a5a7a;font-size:13px;border-top:1px solid #1f2a3f;padding-top:18px;margin-top:20px}
        .badge{display:inline-block;background:#1f3a5f;color:#80cbc4;padding:3px 10px;border-radius:20px;font-size:12px}
        .refresh-btn{background:#1f3a5f;border:none;color:#fff;padding:6px 18px;border-radius:30px;cursor:pointer;font-size:13px}
        .refresh-btn:hover{background:#2a4a7a}
        .flex{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px;margin-bottom:10px}
        @media(max-width:600px){.grid{grid-template-columns:1fr 1fr}.card .value{font-size:22px}}
    </style>
</head>
<body>
<div class="container">
    <div class="flex">
        <h1>⛏️ داشبورد ماینینگ مونرو</h1>
        <button class="refresh-btn" onclick="fetchData()">🔄 بروزرسانی</button>
    </div>
    <div class="grid" id="cards">
        <div class="card"><div class="label">🔹 هش‌ریت (کل)</div><div class="value" id="hr">--</div><div class="sub" id="hr_highest">بیشترین: --</div></div>
        <div class="card"><div class="label">✅ شارهای پذیرفته</div><div class="value" id="shares">--</div><div class="sub" id="shares_total">کل: --</div></div>
        <div class="card"><div class="label">📤 ترافیک ارسال</div><div class="value" id="sent">--</div><div class="sub">مگابایت</div></div>
        <div class="card"><div class="label">📥 ترافیک دریافت</div><div class="value" id="recv">--</div><div class="sub">مگابایت</div></div>
        <div class="card"><div class="label">🕒 آپتایم</div><div class="value" id="uptime">--</div><div class="sub" id="pool">استخر: --</div></div>
        <div class="card"><div class="label">🔗 اتصالات</div><div class="value" id="conns">--</div><div class="sub">آخرین بروزرسانی: <span id="last_upd">--</span></div></div>
    </div>
    <div class="chart-wrap">
        <canvas id="chart"></canvas>
    </div>
    <div class="footer">
        ساخته شده با ❤️ برای ماینینگ مونرو · داده‌ها از XMRig API
    </div>
</div>
<script>
let chartInstance = null;

async function fetchData() {
    try {
        const res = await fetch('/api/stats');
        const data = await res.json();
        document.getElementById('hr').textContent = (data.hashrate.total / 1e6).toFixed(2) + ' MH/s';
        document.getElementById('hr_highest').textContent = 'بیشترین: ' + (data.hashrate.highest / 1e6).toFixed(2) + ' MH/s';
        document.getElementById('shares').textContent = data.shares.good || 0;
        document.getElementById('shares_total').textContent = 'کل: ' + (data.shares.total || 0);
        document.getElementById('sent').textContent = (data.traffic.sent_mb || 0).toFixed(2);
        document.getElementById('recv').textContent = (data.traffic.recv_mb || 0).toFixed(2);
        document.getElementById('uptime').textContent = formatUptime(data.uptime || 0);
        document.getElementById('pool').textContent = 'استخر: ' + (data.pool || 'نامشخص');
        document.getElementById('conns').textContent = data.connections || 0;
        document.getElementById('last_upd').textContent = data.last_update ? new Date(data.last_update).toLocaleTimeString('fa-IR') : '--';
        await loadHistory();
    } catch(e) {
        console.error(e);
    }
}

function formatUptime(sec) {
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const s = sec % 60;
    return h + 'h ' + m + 'm ' + s + 's';
}

async function loadHistory() {
    try {
        const res = await fetch('/api/history');
        const history = await res.json();
        const labels = history.map(p => new Date(p.time).toLocaleTimeString('fa-IR'));
        const values = history.map(p => p.hashrate / 1e6);
        if (chartInstance) {
            chartInstance.data.labels = labels;
            chartInstance.data.datasets[0].data = values;
            chartInstance.update();
        } else {
            const ctx = document.getElementById('chart').getContext('2d');
            chartInstance = new Chart(ctx, {
                type: 'line',
                data: {
                    labels: labels,
                    datasets: [{
                        label: 'هش‌ریت (MH/s)',
                        data: values,
                        borderColor: '#4fc3f7',
                        backgroundColor: 'rgba(79,195,247,0.1)',
                        fill: true,
                        tension: 0.3,
                        pointRadius: 3,
                        pointBackgroundColor: '#4fc3f7'
                    }]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        legend: { labels: { color: '#b0d4ff' } }
                    },
                    scales: {
                        x: { ticks: { color: '#6a7fa0', maxTicksLimit: 12 } },
                        y: { ticks: { color: '#6a7fa0' } }
                    }
                }
            });
        }
    } catch(e) { console.error(e); }
}

// بارگذاری اولیه
fetchData();
setInterval(fetchData, 15000);  // هر ۱۵ ثانیه
</script>
</body>
</html>
"""

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return HTMLResponse(HTML_PAGE)

@app.get("/health")
async def health():
    return {"status": "ok", "last_update": state.get("last_update")}

# ─── اجرا ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, log_level="info")
