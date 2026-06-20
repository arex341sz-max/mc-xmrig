import os
import json
import subprocess
import time
import asyncio
import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── مدل داده برای تنظیم کیف پول ────────────────────────────────────────────
class WalletConfig(BaseModel):
    wallet: str

# ─── وضعیت ماینر ──────────────────────────────────────────────────────────────
miner_process = None
miner_status = {
    "running": False,
    "wallet": "",
    "hashrate": 0,
    "shares": 0,
    "uptime": 0,
    "last_update": None,
}

# ─── توابع مدیریت ماینر ──────────────────────────────────────────────────────
def generate_config(wallet_address: str) -> str:
    """فایل config.json را با کیف پول جدید می‌سازد"""
    template = {
        "autosave": False,
        "cpu": {
            "enabled": True,
            "huge-pages": True,
            "hw-aes": True,
            "max-threads-hint": 50,
            "asm": True
        },
        "pools": [
            {
                "url": "pool.supportxmr.com:443",
                "user": wallet_address,
                "pass": "railway_worker",
                "tls": True,
                "keepalive": True
            }
        ],
        "api": {
            "port": 8080,
            "access-token": None
        },
        "donate-level": 1,
        "opencl": False,
        "cuda": False,
        "print-time": 60,
        "retries": 999,
        "retry-pause": 10
    }
    with open("/app/config.json", "w") as f:
        json.dump(template, f, indent=2)
    return "/app/config.json"

def start_miner(wallet: str):
    global miner_process, miner_status
    # اگر ماینر در حال اجراست، آن را متوقف کن
    if miner_process and miner_process.poll() is None:
        miner_process.terminate()
        time.sleep(2)
        if miner_process.poll() is None:
            miner_process.kill()
        miner_process = None

    # تولید کانفیگ جدید
    config_path = generate_config(wallet)

    # اجرای ماینر با subprocess
    # فرض می‌کنیم xmrig در مسیر /usr/local/bin/xmrig نصب است
    # یا اگر در Docker از image miningcontainers/xmrig استفاده می‌کنیم، مسیر /xmrig/xmrig است.
    xmrig_path = "/xmrig/xmrig"  # مسیر پیش‌فرض در image miningcontainers/xmrig
    if not os.path.exists(xmrig_path):
        # اگر در image ما نصب نشده، از مسیر دیگری استفاده کن
        xmrig_path = "/usr/local/bin/xmrig"

    if not os.path.exists(xmrig_path):
        raise Exception("xmrig executable not found!")

    miner_process = subprocess.Popen(
        [xmrig_path, "-c", config_path],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )
    miner_status["running"] = True
    miner_status["wallet"] = wallet
    miner_status["start_time"] = time.time()
    print(f"✅ ماینر با کیف پول {wallet} راه‌اندازی شد (PID: {miner_process.pid})")

def stop_miner():
    global miner_process, miner_status
    if miner_process and miner_process.poll() is None:
        miner_process.terminate()
        time.sleep(2)
        if miner_process.poll() is None:
            miner_process.kill()
        miner_process = None
    miner_status["running"] = False
    print("⏹️ ماینر متوقف شد")

# ─── دریافت آمار از ماینر ────────────────────────────────────────────────────
async def fetch_stats():
    global miner_status
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get("http://localhost:8080/api/summary")
            if resp.status_code == 200:
                data = resp.json()
                miner_status["hashrate"] = data.get("hashrate", {}).get("total", [0])[0]
                miner_status["shares"] = data.get("results", {}).get("shares_good", 0)
                miner_status["uptime"] = int(time.time() - miner_status.get("start_time", time.time()))
                miner_status["last_update"] = time.time()
    except Exception as e:
        print(f"⚠️ خطا در دریافت آمار: {e}")

# ─── تایمر پس‌زمینه ──────────────────────────────────────────────────────────
async def periodic_fetch():
    while True:
        if miner_status["running"]:
            await fetch_stats()
        await asyncio.sleep(10)

@app.on_event("startup")
async def startup():
    # اگر قبلاً کیف پولی ذخیره شده، آن را بازیابی کن
    # (اختیاری: از فایل state.json بخوان)
    asyncio.create_task(periodic_fetch())
    print("🚀 داشبورد ماینینگ راه‌اندازی شد")

@app.on_event("shutdown")
async def shutdown():
    stop_miner()

# ─── API endpointها ──────────────────────────────────────────────────────────
@app.post("/api/start-mining")
async def start_mining(config: WalletConfig):
    try:
    if not config.wallet or len(config.wallet.strip()) < 5:
    raise HTTPException(status_code=400, detail="آدرس کیف پول معتبر نیست")
        start_miner(config.wallet)
        return {"status": "ok", "message": f"ماینینگ با کیف پول {config.wallet[:8]}... شروع شد"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/stop-mining")
async def stop_mining():
    stop_miner()
    return {"status": "ok", "message": "ماینینگ متوقف شد"}

@app.get("/api/miner-status")
async def get_miner_status():
    return JSONResponse(miner_status)

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
        h1{text-align:center;margin-bottom:20px;color:#4fc3f7}
        .card{background:#141b2b;border-radius:12px;padding:18px;border:1px solid #2a3a5c;margin-bottom:16px}
        .card .label{color:#8899bb;font-weight:600}
        .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:16px}
        .grid .card .value{font-size:26px;font-weight:700;margin-top:6px;color:#b0d4ff}
        .flex{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
        input,button{padding:10px 16px;border-radius:8px;border:1px solid #2a3a5c;background:#0f1729;color:#fff;font-size:14px}
        input{flex:1;min-width:200px}
        button{background:#1f3a5f;cursor:pointer;font-weight:600}
        button:hover{background:#2a4a7a}
        .btn-start{background:#0d7a3b}
        .btn-start:hover{background:#0f9d4a}
        .btn-stop{background:#7a2a2a}
        .btn-stop:hover{background:#a33a3a}
        .badge{display:inline-block;background:#1f3a5f;padding:3px 10px;border-radius:20px;font-size:12px}
        .chart-wrap{background:#141b2b;border-radius:12px;padding:20px;border:1px solid #2a3a5c;margin-top:16px}
        .chart-wrap canvas{width:100% !important;height:280px !important}
        .footer{text-align:center;color:#4a5a7a;font-size:13px;border-top:1px solid #1f2a3f;padding-top:18px;margin-top:20px}
        .status-dot{display:inline-block;width:12px;height:12px;border-radius:50%;margin-left:5px}
        .dot-green{background:#4caf50}
        .dot-red{background:#f44336}
        .dot-yellow{background:#ffeb3b}
    </style>
</head>
<body>
<div class="container">
    <h1>⛏️ داشبورد ماینینگ مونرو</h1>
    
    <div class="card">
        <div class="label">🔑 تنظیم آدرس کیف پول</div>
        <div class="flex" style="margin-top:8px">
            <input type="text" id="walletInput" placeholder="آدرس کیف پول مونرو خود را وارد کنید" value="48edfHu7V9Z84YzzMa6fUueoELZ9ZRXq9VetWzYGzKt52XU5xvqgzYnDK9URnRoJMk1j8nLwEVsaSWJ4fhdUyZijBGUicoD">
            <button class="btn-start" onclick="startMining()">▶️ شروع ماینینگ</button>
            <button class="btn-stop" onclick="stopMining()">⏹️ توقف</button>
        </div>
        <div id="statusMsg" style="margin-top:8px;font-size:13px;color:#80cbc4"></div>
    </div>

    <div class="grid" id="cards">
        <div class="card"><div class="label">🔹 هش‌ریت</div><div class="value" id="hr">--</div><div class="label">وضعیت: <span id="statusText">غیرفعال</span></div></div>
        <div class="card"><div class="label">✅ شارهای پذیرفته</div><div class="value" id="shares">--</div><div class="label">کیف پول: <span id="walletDisplay">--</span></div></div>
        <div class="card"><div class="label">🕒 آپتایم</div><div class="value" id="uptime">--</div><div class="label">آخرین بروزرسانی: <span id="lastUpdate">--</span></div></div>
        <div class="card"><div class="label">🔗 اتصال به استخر</div><div class="value" id="poolStatus">--</div><div class="label" id="poolName">--</div></div>
    </div>

    <div class="chart-wrap">
        <canvas id="chart"></canvas>
    </div>
    <div class="footer">
        ساخته شده با ❤️ · داده‌ها از XMRig API
    </div>
</div>
<script>
let chartInstance = null;
let historyData = [];

async function startMining() {
    const wallet = document.getElementById('walletInput').value.trim();
    if (!wallet || wallet.length < 50) {
        alert('لطفاً آدرس کیف پول معتبر وارد کنید');
        return;
    }
    document.getElementById('statusMsg').innerHTML = '🔄 در حال راه‌اندازی ماینر...';
    try {
        const res = await fetch('/api/start-mining', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({wallet})
        });
        const data = await res.json();
        if (res.ok) {
            document.getElementById('statusMsg').innerHTML = '✅ ' + data.message;
        } else {
            document.getElementById('statusMsg').innerHTML = '❌ خطا: ' + data.detail;
        }
    } catch(e) {
        document.getElementById('statusMsg').innerHTML = '❌ خطا در ارتباط با سرور';
    }
    fetchStatus();
}

async function stopMining() {
    document.getElementById('statusMsg').innerHTML = '⏹️ در حال توقف ماینر...';
    try {
        const res = await fetch('/api/stop-mining', { method: 'POST' });
        const data = await res.json();
        document.getElementById('statusMsg').innerHTML = '✅ ' + data.message;
    } catch(e) {
        document.getElementById('statusMsg').innerHTML = '❌ خطا در توقف';
    }
    fetchStatus();
}

async function fetchStatus() {
    try {
        const res = await fetch('/api/miner-status');
        const data = await res.json();
        document.getElementById('hr').textContent = data.running ? (data.hashrate / 1e6).toFixed(2) + ' MH/s' : '--';
        document.getElementById('shares').textContent = data.shares || '--';
        document.getElementById('uptime').textContent = data.running ? formatUptime(data.uptime) : '--';
        document.getElementById('walletDisplay').textContent = data.wallet ? data.wallet.slice(0,12)+'...' : '--';
        document.getElementById('lastUpdate').textContent = data.last_update ? new Date(data.last_update*1000).toLocaleTimeString('fa-IR') : '--';
        const statusText = document.getElementById('statusText');
        if (data.running) {
            statusText.innerHTML = '<span class="status-dot dot-green"></span> فعال';
        } else {
            statusText.innerHTML = '<span class="status-dot dot-red"></span> غیرفعال';
        }
        // pool status (mock)
        document.getElementById('poolStatus').textContent = data.running ? 'متصل' : 'قطع';
        document.getElementById('poolName').textContent = data.running ? 'pool.supportxmr.com' : '--';
        // اضافه کردن به تاریخچه برای نمودار
        if (data.running && data.hashrate > 0) {
            historyData.push({time: new Date(), hashrate: data.hashrate});
            if (historyData.length > 100) historyData.shift();
            updateChart();
        }
    } catch(e) { console.error(e); }
}

function formatUptime(sec) {
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const s = sec % 60;
    return h + 'h ' + m + 'm ' + s + 's';
}

function updateChart() {
    const labels = historyData.map(p => p.time.toLocaleTimeString('fa-IR'));
    const values = historyData.map(p => p.hashrate / 1e6);
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
                plugins: { legend: { labels: { color: '#b0d4ff' } } },
                scales: {
                    x: { ticks: { color: '#6a7fa0', maxTicksLimit: 12 } },
                    y: { ticks: { color: '#6a7fa0' } }
                }
            }
        });
    }
}

// بارگذاری اولیه و تایمر
fetchStatus();
setInterval(fetchStatus, 10000); // هر ۱۰ ثانیه
</script>
</body>
</html>
"""

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return HTMLResponse(HTML_PAGE)

@app.get("/health")
async def health():
    return {"status": "ok"}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
