import os
import json
import subprocess
import time
import asyncio
import httpx
import signal
from datetime import datetime
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn

# ─── راه‌اندازی اپلیکیشن ──────────────────────────────────────────────────────
app = FastAPI(title="Monero Mining Dashboard", version="2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── مدل داده ──────────────────────────────────────────────────────────────────
class WalletConfig(BaseModel):
    wallet: str

# ─── وضعیت ماینر ──────────────────────────────────────────────────────────────
miner_process = None
miner_status = {
    "running": False,
    "wallet": "",
    "hashrate": 0,
    "hashrate_highest": 0,
    "shares_good": 0,
    "shares_total": 0,
    "shares_rejected": 0,
    "uptime": 0,
    "start_time": None,
    "last_update": None,
    "pool": "",
    "error": None,
    "connected": False,
}
history = []  # برای نمودار

# ─── توابع مدیریت ماینر ──────────────────────────────────────────────────────
def generate_config(wallet_address: str) -> str:
    """تولید فایل config.json با تنظیمات بهینه برای Railway"""
    template = {
        "autosave": False,
        "cpu": {
            "enabled": True,
            "huge-pages": False,
            "hw-aes": True,
            "max-threads-hint": 1,
            "asm": True,
            "priority": 5
        },
        "pools": [
            {
                "url": "pool.supportxmr.com:443",
                "user": wallet_address,
                "pass": "railway_worker",
                "tls": True,
                "keepalive": True,
                "nicehash": False
            }
        ],
        "api": {
            "port": 8080,
            "access-token": None,
            "worker-id": "railway-miner",
            "ipv6": False
        },
        "http": {
            "enabled": True,
            "port": 8080,
            "access-token": None,
            "restricted": True
        },
        "donate-level": 1,
        "opencl": False,
        "cuda": False,
        "print-time": 60,
        "retries": 999,
        "retry-pause": 10,
        "health-print-time": 60
    }
    config_path = "/app/config.json"
    with open(config_path, "w") as f:
        json.dump(template, f, indent=2)
    return config_path

def start_miner(wallet: str):
    global miner_process, miner_status
    
    # توقف ماینر قبلی
    if miner_process and miner_process.poll() is None:
        miner_process.terminate()
        time.sleep(2)
        if miner_process.poll() is None:
            miner_process.kill()
        miner_process = None

    config_path = generate_config(wallet)

    # پیدا کردن فایل اجرایی XMRig
    xmrig_path = "/usr/local/bin/xmrig"
    if not os.path.exists(xmrig_path):
        xmrig_path = "/xmrig/build/xmrig"
        if not os.path.exists(xmrig_path):
            miner_status["error"] = "xmrig executable not found!"
            raise Exception("xmrig executable not found!")

    if not os.access(xmrig_path, os.X_OK):
        os.chmod(xmrig_path, 0o755)

    try:
        # اجرای ماینر
        miner_process = subprocess.Popen(
            [xmrig_path, "-c", config_path, "--donate-level=1"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1
        )
        
        # به‌روزرسانی وضعیت
        miner_status["running"] = True
        miner_status["wallet"] = wallet
        miner_status["start_time"] = time.time()
        miner_status["uptime"] = 0
        miner_status["error"] = None
        miner_status["hashrate"] = 0
        miner_status["shares_good"] = 0
        miner_status["connected"] = False
        
        print(f"✅ ماینر با کیف پول {wallet[:8]}... راه‌اندازی شد (PID: {miner_process.pid})")
        
        # شروع تسک‌های پس‌زمینه
        asyncio.create_task(wait_for_api())
        asyncio.create_task(monitor_miner())
        
    except Exception as e:
        miner_status["running"] = False
        miner_status["error"] = str(e)
        print(f"❌ خطا در راه‌اندازی ماینر: {e}")
        raise

async def wait_for_api():
    """منتظر می‌ماند تا API ماینر فعال شود"""
    for i in range(30):
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                resp = await client.get("http://localhost:8080/api/summary")
                if resp.status_code == 200:
                    miner_status["connected"] = True
                    print("✅ API ماینر فعال شد")
                    return
        except:
            pass
        await asyncio.sleep(1)
    print("⚠️ API ماینر پس از 30 ثانیه فعال نشد")

async def monitor_miner():
    """پایش خروجی ماینر برای تشخیص خطاها"""
    global miner_process, miner_status
    if not miner_process:
        return
    
    try:
        while miner_process and miner_process.poll() is None:
            line = await asyncio.to_thread(miner_process.stderr.readline)
            if line:
                line = line.strip()
                print(f"[XMRig] {line}")
                
                # تشخیص رویدادهای مهم
                if "accepted" in line.lower():
                    miner_status["shares_good"] += 1
                elif "reject" in line.lower():
                    miner_status["shares_rejected"] += 1
                elif "error" in line.lower() or "failed" in line.lower():
                    miner_status["error"] = line
                elif "connected" in line.lower():
                    miner_status["connected"] = True
                    
            await asyncio.sleep(0.1)
        
        # اگر ماینر متوقف شد
        if miner_process and miner_process.poll() is not None:
            exit_code = miner_process.poll()
            print(f"⚠️ ماینر با کد {exit_code} متوقف شد")
            miner_status["running"] = False
            miner_status["connected"] = False
            if exit_code != 0:
                miner_status["error"] = f"Exit code: {exit_code}"
            
    except Exception as e:
        print(f"⚠️ خطا در پایش ماینر: {e}")

def stop_miner():
    global miner_process, miner_status
    if miner_process and miner_process.poll() is None:
        miner_process.terminate()
        time.sleep(2)
        if miner_process.poll() is None:
            miner_process.kill()
        miner_process = None
    miner_status["running"] = False
    miner_status["connected"] = False
    print("⏹️ ماینر متوقف شد")

# ─── دریافت آمار از ماینر ────────────────────────────────────────────────────
async def fetch_stats():
    global miner_status, history
    
    if not miner_status["running"]:
        return
        
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            # امتحان مسیرهای مختلف API
            for path in ["/api/summary", "/summary", "/2/summary"]:
                try:
                    resp = await client.get(f"http://localhost:8080{path}")
                    if resp.status_code == 200:
                        data = resp.json()
                        
                        # استخراج داده‌ها
                        hashrate = data.get("hashrate", {}).get("total", [0])[0]
                        if hashrate > miner_status["hashrate_highest"]:
                            miner_status["hashrate_highest"] = hashrate
                            
                        miner_status["hashrate"] = hashrate
                        miner_status["shares_total"] = data.get("results", {}).get("shares_total", 0)
                        miner_status["pool"] = data.get("pool", "")
                        miner_status["uptime"] = int(time.time() - miner_status.get("start_time", time.time()))
                        miner_status["last_update"] = time.time()
                        
                        # ذخیره تاریخچه برای نمودار
                        history.append({
                            "time": datetime.now().isoformat(),
                            "hashrate": hashrate
                        })
                        if len(history) > 100:
                            history = history[-100:]
                        
                        print(f"📊 هش: {hashrate/1e3:.0f} H/s | شار خوب: {miner_status['shares_good']}")
                        return
                except:
                    continue
                    
    except Exception as e:
        print(f"⚠️ خطا در دریافت آمار: {e}")

async def periodic_fetch():
    """دریافت آمار به صورت دوره‌ای"""
    while True:
        await fetch_stats()
        await asyncio.sleep(10)

# ─── رویدادهای شروع و پایان ──────────────────────────────────────────────────
@app.on_event("startup")
async def startup():
    signal.signal(signal.SIGTERM, lambda sig, frame: None)
    asyncio.create_task(periodic_fetch())
    print("🚀 داشبورد ماینینگ راه‌اندازی شد")
    print("📌 برای شروع، آدرس کیف پول مونرو خود را وارد کنید")

@app.on_event("shutdown")
async def shutdown():
    stop_miner()
    print("👋 اپلیکیشن متوقف شد")

# ─── API Endpointها ───────────────────────────────────────────────────────────
@app.post("/api/start-mining")
async def start_mining(config: WalletConfig):
    if not config.wallet or len(config.wallet.strip()) < 5:
        raise HTTPException(status_code=400, detail="لطفاً آدرس کیف پول را وارد کنید")
    
    # بررسی اینکه آیا آدرس مونرو است (حدود 95 کاراکتر، شروع با 4)
    if len(config.wallet.strip()) < 90:
        return {
            "status": "warning", 
            "message": "⚠️ این آدرس کوتاه‌تر از آدرس استاندارد مونرو است. آیا مطمئن هستید؟"
        }
    
    try:
        start_miner(config.wallet)
        return {"status": "ok", "message": f"✅ ماینینگ با کیف پول {config.wallet[:10]}... شروع شد"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/stop-mining")
async def stop_mining():
    stop_miner()
    return {"status": "ok", "message": "⏹️ ماینینگ متوقف شد"}

@app.get("/api/miner-status")
async def get_miner_status():
    return JSONResponse(miner_status)

@app.get("/api/history")
async def get_history():
    return JSONResponse(history[-100:])

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "miner_running": miner_status["running"],
        "connected": miner_status["connected"],
        "uptime": miner_status["uptime"]
    }

# ─── صفحه HTML داشبورد ───────────────────────────────────────────────────────
HTML_PAGE = """
<!DOCTYPE html>
<html lang="fa" dir="rtl">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>⛏️ داشبورد ماینینگ مونرو</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        :root {
            --bg: #0a0e1a;
            --card: #12182b;
            --border: #1e2a45;
            --primary: #4fc3f7;
            --success: #4caf50;
            --danger: #ef5350;
            --warning: #ffa726;
            --text: #e0e8f0;
            --text-dim: #6a7fa0;
            --radius: 14px;
        }
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: var(--bg);
            color: var(--text);
            padding: 20px;
            direction: rtl;
            min-height: 100vh;
        }
        .container { max-width: 1300px; margin: 0 auto; }
        
        /* هدر */
        .header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 16px;
            margin-bottom: 28px;
            padding-bottom: 16px;
            border-bottom: 1px solid var(--border);
        }
        .header h1 {
            font-size: 26px;
            font-weight: 700;
            color: var(--primary);
            display: flex;
            align-items: center;
            gap: 10px;
        }
        .header h1 i { font-size: 28px; }
        .header-info {
            display: flex;
            align-items: center;
            gap: 12px;
            flex-wrap: wrap;
        }
        .status-badge {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            padding: 6px 14px;
            border-radius: 20px;
            font-size: 13px;
            font-weight: 600;
        }
        .status-badge.online {
            background: rgba(76, 175, 80, 0.15);
            color: var(--success);
            border: 1px solid rgba(76, 175, 80, 0.3);
        }
        .status-badge.offline {
            background: rgba(239, 83, 80, 0.15);
            color: var(--danger);
            border: 1px solid rgba(239, 83, 80, 0.3);
        }
        .status-badge .dot {
            width: 8px;
            height: 8px;
            border-radius: 50%;
            display: inline-block;
        }
        .status-badge .dot.online { background: var(--success); animation: pulse 2s infinite; }
        .status-badge .dot.offline { background: var(--danger); }
        
        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.3; }
        }
        
        /* کارت‌ها */
        .card {
            background: var(--card);
            border: 1px solid var(--border);
            border-radius: var(--radius);
            padding: 20px 24px;
            margin-bottom: 18px;
            transition: border-color 0.3s;
        }
        .card:hover { border-color: rgba(79, 195, 247, 0.2); }
        .card-title {
            font-size: 13px;
            font-weight: 600;
            color: var(--text-dim);
            margin-bottom: 10px;
            display: flex;
            align-items: center;
            gap: 8px;
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }
        
        /* فرم ورودی کیف پول */
        .wallet-section {
            display: flex;
            flex-wrap: wrap;
            gap: 12px;
            align-items: center;
        }
        .wallet-section input {
            flex: 1;
            min-width: 280px;
            padding: 12px 18px;
            border-radius: 10px;
            border: 1px solid var(--border);
            background: rgba(255,255,255,0.04);
            color: var(--text);
            font-size: 14px;
            font-family: monospace;
            transition: border-color 0.3s;
            outline: none;
            letter-spacing: 0.3px;
        }
        .wallet-section input:focus {
            border-color: var(--primary);
            box-shadow: 0 0 0 3px rgba(79, 195, 247, 0.1);
        }
        .wallet-section input::placeholder {
            color: var(--text-dim);
            font-family: 'Segoe UI', sans-serif;
        }
        .btn {
            padding: 12px 24px;
            border-radius: 10px;
            border: none;
            font-weight: 600;
            font-size: 14px;
            cursor: pointer;
            transition: all 0.2s;
            display: inline-flex;
            align-items: center;
            gap: 8px;
        }
        .btn-start {
            background: linear-gradient(135deg, #1b8a3b, #0d6b2a);
            color: #fff;
        }
        .btn-start:hover { transform: translateY(-2px); box-shadow: 0 4px 20px rgba(27, 138, 59, 0.35); }
        .btn-stop {
            background: linear-gradient(135deg, #b71c1c, #7f0000);
            color: #fff;
        }
        .btn-stop:hover { transform: translateY(-2px); box-shadow: 0 4px 20px rgba(183, 28, 28, 0.35); }
        .btn-refresh {
            background: rgba(79, 195, 247, 0.12);
            color: var(--primary);
            border: 1px solid rgba(79, 195, 247, 0.15);
        }
        .btn-refresh:hover { background: rgba(79, 195, 247, 0.2); }
        
        /* متریک‌ها */
        .metrics {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
            gap: 16px;
            margin-bottom: 18px;
        }
        .metric {
            background: var(--card);
            border: 1px solid var(--border);
            border-radius: var(--radius);
            padding: 18px 20px;
            transition: all 0.3s;
        }
        .metric:hover {
            border-color: rgba(79, 195, 247, 0.25);
            transform: translateY(-2px);
        }
        .metric-label {
            font-size: 11px;
            color: var(--text-dim);
            text-transform: uppercase;
            letter-spacing: 0.06em;
            font-weight: 600;
        }
        .metric-value {
            font-size: 28px;
            font-weight: 700;
            margin-top: 4px;
            color: var(--text);
        }
        .metric-value .unit {
            font-size: 14px;
            font-weight: 400;
            color: var(--text-dim);
            margin-right: 4px;
        }
        .metric-sub {
            font-size: 11px;
            color: var(--text-dim);
            margin-top: 4px;
        }
        
        /* نمودار */
        .chart-container {
            background: var(--card);
            border: 1px solid var(--border);
            border-radius: var(--radius);
            padding: 20px 24px;
            margin-top: 10px;
        }
        .chart-container canvas {
            width: 100% !important;
            height: 280px !important;
        }
        
        /* خطا */
        .error-box {
            background: rgba(239, 83, 80, 0.08);
            border: 1px solid rgba(239, 83, 80, 0.2);
            border-radius: 10px;
            padding: 12px 16px;
            margin-top: 12px;
            color: var(--danger);
            font-size: 13px;
            display: none;
            align-items: center;
            gap: 10px;
        }
        .error-box.show { display: flex; }
        
        /* وضعیت اتصال */
        .connection-info {
            display: flex;
            align-items: center;
            gap: 8px;
            font-size: 13px;
            color: var(--text-dim);
        }
        .connection-info .connected {
            color: var(--success);
        }
        .connection-info .disconnected {
            color: var(--danger);
        }
        
        /* فوتر */
        .footer {
            text-align: center;
            color: var(--text-dim);
            font-size: 12px;
            padding-top: 20px;
            border-top: 1px solid var(--border);
            margin-top: 20px;
        }
        .footer a {
            color: var(--primary);
            text-decoration: none;
        }
        
        /* واکنش‌گرا */
        @media (max-width: 640px) {
            body { padding: 12px; }
            .header h1 { font-size: 20px; }
            .metric-value { font-size: 22px; }
            .wallet-section input { min-width: 160px; }
            .btn { padding: 10px 16px; font-size: 13px; }
        }
    </style>
</head>
<body>
<div class="container">
    <!-- هدر -->
    <div class="header">
        <h1><i class="fas fa-microchip"></i> ⛏️ ماینینگ مونرو</h1>
        <div class="header-info">
            <span class="status-badge" id="statusBadge">
                <span class="dot offline" id="statusDot"></span>
                <span id="statusText">غیرفعال</span>
            </span>
            <button class="btn btn-refresh" onclick="fetchAll()">
                <i class="fas fa-sync"></i> بروزرسانی
            </button>
        </div>
    </div>

    <!-- کارت تنظیم کیف پول -->
    <div class="card">
        <div class="card-title"><i class="fas fa-wallet"></i> 🔑 کیف پول</div>
        <div class="wallet-section">
            <input type="text" id="walletInput" 
                   placeholder="آدرس کیف پول مونرو خود را وارد کنید (حدود 95 کاراکتر)" 
                   value="48edfHu7V9Z84YzzMa6fUueoELZ9ZRXq9VetWzYGzKt52XU5xvqgzYnDK9URnRoJMk1j8nLwEVsaSWJ4fhdUyZijBGUicoD">
            <button class="btn btn-start" onclick="startMining()">
                <i class="fas fa-play"></i> شروع
            </button>
            <button class="btn btn-stop" onclick="stopMining()">
                <i class="fas fa-stop"></i> توقف
            </button>
        </div>
        <div id="statusMsg" style="margin-top:10px;font-size:13px;color:var(--text-dim)"></div>
        <div class="error-box" id="errorBox">
            <i class="fas fa-exclamation-circle"></i>
            <span id="errorText"></span>
        </div>
    </div>

    <!-- متریک‌ها -->
    <div class="metrics" id="metrics">
        <div class="metric">
            <div class="metric-label"><i class="fas fa-tachometer-alt"></i> هش‌ریت</div>
            <div class="metric-value" id="hashrate">-- <span class="unit">H/s</span></div>
            <div class="metric-sub">بیشترین: <span id="hashrateHighest">--</span></div>
        </div>
        <div class="metric">
            <div class="metric-label"><i class="fas fa-check-circle"></i> شارهای پذیرفته</div>
            <div class="metric-value" id="sharesGood">--</div>
            <div class="metric-sub">کل: <span id="sharesTotal">--</span> | رد: <span id="sharesRejected">--</span></div>
        </div>
        <div class="metric">
            <div class="metric-label"><i class="fas fa-clock"></i> آپتایم</div>
            <div class="metric-value" id="uptime">--</div>
            <div class="metric-sub">آخرین بروزرسانی: <span id="lastUpdate">--</span></div>
        </div>
        <div class="metric">
            <div class="metric-label"><i class="fas fa-plug"></i> اتصال</div>
            <div class="metric-value" id="poolStatus">--</div>
            <div class="metric-sub" id="poolName">استخر: --</div>
        </div>
    </div>

    <!-- نمودار -->
    <div class="chart-container">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;flex-wrap:wrap;gap:8px;">
            <div class="card-title" style="margin-bottom:0;"><i class="fas fa-chart-line"></i> نمودار هش‌ریت</div>
            <span style="font-size:11px;color:var(--text-dim);">کیف پول: <span id="walletDisplay" style="font-family:monospace;">--</span></span>
        </div>
        <canvas id="chart"></canvas>
    </div>

    <!-- فوتر -->
    <div class="footer">
        <i class="fas fa-bolt"></i> بهینه‌سازی شده برای Railway · 
        <i class="fas fa-memory"></i> مصرف رم &lt; 1GB · 
        <i class="fas fa-heart" style="color:var(--danger);"></i> 
        <a href="https://t.me/CodeBoxo" target="_blank">@CodeBoxo</a>
    </div>
</div>

<script>
// ─── متغیرهای عمومی ──────────────────────────────────────────────────────────
let chartInstance = null;
let historyData = [];
let refreshInterval = null;

// ─── شروع ماینینگ ────────────────────────────────────────────────────────────
async function startMining() {
    const wallet = document.getElementById('walletInput').value.trim();
    if (!wallet || wallet.length < 5) {
        alert('لطفاً آدرس کیف پول خود را وارد کنید');
        return;
    }
    document.getElementById('statusMsg').innerHTML = '🔄 در حال راه‌اندازی ماینر...';
    document.getElementById('errorBox').classList.remove('show');
    
    try {
        const res = await fetch('/api/start-mining', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ wallet })
        });
        const data = await res.json();
        
        if (res.ok) {
            document.getElementById('statusMsg').innerHTML = '✅ ' + data.message;
        } else {
            document.getElementById('statusMsg').innerHTML = '❌ خطا: ' + data.detail;
            showError(data.detail);
        }
    } catch (e) {
        document.getElementById('statusMsg').innerHTML = '❌ خطا در ارتباط با سرور';
        showError(e.message);
    }
    fetchAll();
}

// ─── توقف ماینینگ ────────────────────────────────────────────────────────────
async function stopMining() {
    document.getElementById('statusMsg').innerHTML = '⏹️ در حال توقف...';
    try {
        const res = await fetch('/api/stop-mining', { method: 'POST' });
        const data = await res.json();
        document.getElementById('statusMsg').innerHTML = '✅ ' + data.message;
    } catch (e) {
        document.getElementById('statusMsg').innerHTML = '❌ خطا در توقف';
    }
    fetchAll();
}

// ─── دریافت وضعیت ─────────────────────────────────────────────────────────────
async function fetchStatus() {
    try {
        const res = await fetch('/api/miner-status');
        const data = await res.json();
        updateUI(data);
    } catch (e) {
        console.error('خطا در دریافت وضعیت:', e);
    }
}

// ─── دریافت تاریخچه ──────────────────────────────────────────────────────────
async function fetchHistory() {
    try {
        const res = await fetch('/api/history');
        historyData = await res.json();
        updateChart();
    } catch (e) {
        console.error('خطا در دریافت تاریخچه:', e);
    }
}

// ─── بروزرسانی UI ────────────────────────────────────────────────────────────
function updateUI(data) {
    // هش‌ریت
    const hashrate = data.hashrate || 0;
    document.getElementById('hashrate').innerHTML = 
        hashrate > 0 ? (hashrate/1e3).toFixed(1) + ' <span class="unit">KH/s</span>' : '-- <span class="unit">H/s</span>';
    
    const highest = data.hashrate_highest || 0;
    document.getElementById('hashrateHighest').textContent = 
        highest > 0 ? (highest/1e3).toFixed(1) + ' KH/s' : '--';
    
    // شارها
    document.getElementById('sharesGood').textContent = data.shares_good || 0;
    document.getElementById('sharesTotal').textContent = data.shares_total || 0;
    document.getElementById('sharesRejected').textContent = data.shares_rejected || 0;
    
    // آپتایم
    document.getElementById('uptime').textContent = data.running ? formatUptime(data.uptime) : '--';
    document.getElementById('lastUpdate').textContent = data.last_update ? 
        new Date(data.last_update * 1000).toLocaleTimeString('fa-IR') : '--';
    
    // کیف پول
    document.getElementById('walletDisplay').textContent = data.wallet ? 
        data.wallet.slice(0, 12) + '...' : '--';
    
    // استخر
    document.getElementById('poolStatus').textContent = data.connected ? '🟢 متصل' : '🔴 قطع';
    document.getElementById('poolName').textContent = data.pool ? 'استخر: ' + data.pool : 'استخر: --';
    
    // وضعیت
    const badge = document.getElementById('statusBadge');
    const dot = document.getElementById('statusDot');
    const text = document.getElementById('statusText');
    
    if (data.running && data.connected) {
        badge.className = 'status-badge online';
        dot.className = 'dot online';
        text.textContent = '⛏️ در حال استخراج';
    } else if (data.running) {
        badge.className = 'status-badge online';
        dot.className = 'dot online';
        text.textContent = '🔄 در حال اتصال...';
    } else {
        badge.className = 'status-badge offline';
        dot.className = 'dot offline';
        text.textContent = '⏹️ غیرفعال';
    }
    
    // خطا
    if (data.error) {
        showError(data.error);
    } else {
        document.getElementById('errorBox').classList.remove('show');
    }
    
    // بروزرسانی تاریخچه
    if (data.running && data.hashrate > 0) {
        historyData.push({ time: new Date().toISOString(), hashrate: data.hashrate });
        if (historyData.length > 100) historyData.shift();
        updateChart();
    }
}

// ─── نمایش خطا ───────────────────────────────────────────────────────────────
function showError(msg) {
    const box = document.getElementById('errorBox');
    document.getElementById('errorText').textContent = msg;
    box.classList.add('show');
}

// ─── فرمت آپتایم ──────────────────────────────────────────────────────────────
function formatUptime(sec) {
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const s = sec % 60;
    return h + 'h ' + m + 'm ' + s + 's';
}

// ─── بروزرسانی نمودار ────────────────────────────────────────────────────────
function updateChart() {
    const labels = historyData.map(p => new Date(p.time).toLocaleTimeString('fa-IR'));
    const values = historyData.map(p => p.hashrate / 1e3); // تبدیل به KH/s
    
    const ctx = document.getElementById('chart').getContext('2d');
    
    if (chartInstance) {
        chartInstance.data.labels = labels;
        chartInstance.data.datasets[0].data = values;
        chartInstance.update('none');
    } else {
        chartInstance = new Chart(ctx, {
            type: 'line',
            data: {
                labels: labels,
                datasets: [{
                    label: 'هش‌ریت (KH/s)',
                    data: values,
                    borderColor: '#4fc3f7',
                    backgroundColor: 'rgba(79, 195, 247, 0.08)',
                    fill: true,
                    tension: 0.4,
                    pointRadius: 2,
                    pointBackgroundColor: '#4fc3f7',
                    borderWidth: 2
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: {
                        labels: { color: '#6a7fa0', font: { size: 11 } }
                    }
                },
                scales: {
                    x: {
                        ticks: { color: '#6a7fa0', font: { size: 9 }, maxTicksLimit: 15 },
                        grid: { color: 'rgba(255,255,255,0.03)' }
                    },
                    y: {
                        ticks: { color: '#6a7fa0', font: { size: 9 } },
                        grid: { color: 'rgba(255,255,255,0.03)' },
                        beginAtZero: true
                    }
                },
                interaction: {
                    intersect: false,
                    mode: 'index'
                }
            }
        });
    }
}

// ─── بروزرسانی همه ───────────────────────────────────────────────────────────
async function fetchAll() {
    await fetchStatus();
    await fetchHistory();
}

// ─── راه‌اندازی ──────────────────────────────────────────────────────────────
fetchAll();
refreshInterval = setInterval(fetchAll, 8081);
</script>
</body>
</html>
"""

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return HTMLResponse(HTML_PAGE)

# ─── اجرا ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8081))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
