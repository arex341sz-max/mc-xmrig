import os
import json
import subprocess
import time
import asyncio
import httpx
import signal
import psutil
import socket
import ssl
from datetime import datetime
from fastapi import FastAPI, HTTPException
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

class WalletConfig(BaseModel):
    wallet: str

# ═══════════════════════════════════════════════════════════════
# 🌐 لیست کامل استخرهای مونرو (به‌روز و تست‌شده)
# ═══════════════════════════════════════════════════════════════
MINING_POOLS = [
    {"name": "SupportXMR", "url": "pool.supportxmr.com", "ports": [443, 3333, 5555, 7777, 9000], "tls": True},
    {"name": "MoneroOcean", "url": "gulf.moneroocean.stream", "ports": [10128, 10128], "tls": True},
    {"name": "Nanopool", "url": "xmr.nanopool.org", "ports": [14433, 14444], "tls": True},
    {"name": "HashVault", "url": "pool.hashvault.pro", "ports": [443, 3333, 5555, 7777, 9000], "tls": True},
    {"name": "OMINE", "url": "xmr.omine.ga", "ports": [3000, 5000, 7000, 9000], "tls": True},
]

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
    "pool_name": "",
    "error": None,
    "connected": False,
    "memory_usage_mb": 0,
    "pool_test_results": [],
    "best_pool": None,
}
history = []

# ═══════════════════════════════════════════════════════════════
# 🔍 توابع تست استخرها
# ═══════════════════════════════════════════════════════════════
async def test_pool_connection(pool: dict) -> dict:
    """اتصال به استخر را با تمام پورت‌ها تست می‌کند و بهترین پورت را پیدا می‌کند"""
    result = {
        "name": pool["name"],
        "url": pool["url"],
        "working": False,
        "working_ports": [],
        "best_port": None,
        "response_time": None,
        "error": None
    }
    
    for port in pool["ports"]:
        try:
            start_time = time.time()
            
            if pool.get("tls", False) and port == 443:
                # تست SSL برای پورت 443
                context = ssl.create_default_context()
                with socket.create_connection((pool["url"], port), timeout=5) as sock:
                    with context.wrap_socket(sock, server_hostname=pool["url"]) as ssock:
                        response_time = time.time() - start_time
                        result["working"] = True
                        result["working_ports"].append(port)
                        if result["best_port"] is None or response_time < result.get("response_time", 999):
                            result["best_port"] = port
                            result["response_time"] = response_time
            else:
                # تست معمولی TCP
                with socket.create_connection((pool["url"], port), timeout=5) as sock:
                    response_time = time.time() - start_time
                    result["working"] = True
                    result["working_ports"].append(port)
                    if result["best_port"] is None or response_time < result.get("response_time", 999):
                        result["best_port"] = port
                        result["response_time"] = response_time
                        
        except Exception as e:
            continue
    
    return result

async def test_all_pools():
    """همه استخرها را تست می‌کند و بهترین را انتخاب می‌کند"""
    results = []
    print("🔍 در حال تست اتصال به استخرها...")
    
    for pool in MINING_POOLS:
        print(f"  📡 تست {pool['name']} ({pool['url']})...")
        result = await test_pool_connection(pool)
        results.append(result)
        
        if result["working"]:
            print(f"    ✅ {pool['name']} کار می‌کند! پورت: {result['best_port']} (زمان: {result['response_time']:.2f}s)")
        else:
            print(f"    ❌ {pool['name']} پاسخ نمی‌دهد")
    
    # انتخاب بهترین استخر (کمترین زمان پاسخ)
    working_pools = [r for r in results if r["working"]]
    if working_pools:
        best = min(working_pools, key=lambda x: x.get("response_time", 999))
        best_pool = {
            "name": best["name"],
            "url": best["url"],
            "port": best["best_port"],
            "tls": True if best["best_port"] == 443 else False,
            "response_time": best["response_time"]
        }
        miner_status["best_pool"] = best_pool
        print(f"🏆 بهترین استخر: {best_pool['name']} ({best_pool['url']}:{best_pool['port']})")
    else:
        miner_status["best_pool"] = None
        print("❌ هیچ استخری پاسخ نداد!")
    
    miner_status["pool_test_results"] = results
    return results

# ═══════════════════════════════════════════════════════════════
# ⚙️ توابع مدیریت ماینر
# ═══════════════════════════════════════════════════════════════
def get_process_memory():
    if miner_process and miner_process.pid:
        try:
            proc = psutil.Process(miner_process.pid)
            return round(proc.memory_info().rss / 1024 / 1024, 1)
        except:
            return 0
    return 0

def generate_config(wallet_address: str) -> str:
    """تنظیمات با انتخاب بهترین استخر"""
    best = miner_status.get("best_pool")
    if best:
        pool_url = f"{best['url']}:{best['port']}"
        pool_name = best['name']
    else:
        pool_url = "pool.supportxmr.com:443"
        pool_name = "SupportXMR (پیش‌فرض)"
    
    template = {
        "autosave": False,
        "cpu": {
            "enabled": True,
            "huge-pages": False,
            "hw-aes": True,
            "max-threads-hint": 0.5,
            "asm": True,
            "priority": 5,
            "mode": "light"
        },
        "pools": [
            {
                "url": pool_url,
                "user": wallet_address,
                "pass": "railway_worker",
                "tls": True if best and best.get("tls", False) else False,
                "keepalive": True,
                "nicehash": False,
                "enabled": True
            }
        ],
        "api": {
            "port": 8081,
            "access-token": None,
            "worker-id": "railway-miner"
        },
        "http": {
            "enabled": True,
            "port": 8081,
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
    
    miner_status["pool"] = pool_url
    miner_status["pool_name"] = pool_name
    return config_path

def start_miner(wallet: str):
    global miner_process, miner_status
    
    if miner_process and miner_process.poll() is None:
        miner_process.terminate()
        time.sleep(2)
        if miner_process.poll() is None:
            miner_process.kill()
        miner_process = None

    # اگر بهترین استخر انتخاب نشده، تست کن
    if not miner_status.get("best_pool"):
        asyncio.create_task(test_and_select_best_pool())
        time.sleep(2)

    config_path = generate_config(wallet)

    xmrig_path = "/usr/local/bin/xmrig"
    if not os.path.exists(xmrig_path):
        xmrig_path = "/xmrig/build/xmrig"
        if not os.path.exists(xmrig_path):
            miner_status["error"] = "xmrig not found!"
            raise Exception("xmrig executable not found!")

    if not os.access(xmrig_path, os.X_OK):
        os.chmod(xmrig_path, 0o755)

    try:
        miner_process = subprocess.Popen(
            [xmrig_path, "-c", config_path, "--donate-level=1"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1
        )
        
        miner_status["running"] = True
        miner_status["wallet"] = wallet
        miner_status["start_time"] = time.time()
        miner_status["error"] = None
        miner_status["hashrate"] = 0
        miner_status["shares_good"] = 0
        miner_status["connected"] = False
        miner_status["memory_usage_mb"] = 0
        
        print(f"✅ ماینر با کیف پول {wallet[:8]}... راه‌اندازی شد")
        print(f"📍 استخر: {miner_status['pool_name']} ({miner_status['pool']})")
        
        asyncio.create_task(wait_for_api())
        asyncio.create_task(monitor_miner())
        
    except Exception as e:
        miner_status["running"] = False
        miner_status["error"] = str(e)
        print(f"❌ خطا: {e}")
        raise

async def test_and_select_best_pool():
    """تست استخرها و انتخاب بهترین"""
    results = await test_all_pools()
    if miner_status.get("best_pool"):
        print(f"🏆 بهترین استخر انتخاب شد: {miner_status['best_pool']['name']}")

async def wait_for_api():
    for i in range(30):
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                resp = await client.get("http://localhost:8081/api/summary")
                if resp.status_code == 200:
                    miner_status["connected"] = True
                    print("✅ API ماینر فعال شد")
                    return
        except:
            pass
        await asyncio.sleep(1)
    print("⚠️ API ماینر فعال نشد")

async def monitor_miner():
    global miner_process, miner_status
    if not miner_process:
        return
    
    try:
        while miner_process and miner_process.poll() is None:
            line = await asyncio.to_thread(miner_process.stderr.readline)
            if line:
                line = line.strip()
                print(f"[XMRig] {line}")
                
                if "accepted" in line.lower():
                    miner_status["shares_good"] += 1
                elif "reject" in line.lower():
                    miner_status["shares_rejected"] += 1
                elif "error" in line.lower() or "failed" in line.lower():
                    miner_status["error"] = line
                elif "connected" in line.lower():
                    miner_status["connected"] = True
                    
            miner_status["memory_usage_mb"] = get_process_memory()
            await asyncio.sleep(0.1)
        
        if miner_process and miner_process.poll() is not None:
            exit_code = miner_process.poll()
            print(f"⚠️ ماینر با کد {exit_code} متوقف شد")
            miner_status["running"] = False
            miner_status["connected"] = False
            miner_status["memory_usage_mb"] = 0
            if exit_code != 0:
                miner_status["error"] = f"Exit code: {exit_code}"
            
    except Exception as e:
        print(f"⚠️ خطا در پایش: {e}")

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
    miner_status["memory_usage_mb"] = 0
    print("⏹️ ماینر متوقف شد")

async def fetch_stats():
    global miner_status, history
    
    if not miner_status["running"]:
        return
        
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            for path in ["/api/summary", "/summary", "/2/summary"]:
                try:
                    resp = await client.get(f"http://localhost:8081{path}")
                    if resp.status_code == 200:
                        data = resp.json()
                        
                        hashrate = data.get("hashrate", {}).get("total", [0])[0]
                        if hashrate > miner_status["hashrate_highest"]:
                            miner_status["hashrate_highest"] = hashrate
                            
                        miner_status["hashrate"] = hashrate
                        miner_status["shares_total"] = data.get("results", {}).get("shares_total", 0)
                        miner_status["uptime"] = int(time.time() - miner_status.get("start_time", time.time()))
                        miner_status["last_update"] = time.time()
                        
                        history.append({
                            "time": datetime.now().isoformat(),
                            "hashrate": hashrate
                        })
                        if len(history) > 100:
                            history = history[-100:]
                        
                        print(f"📊 هش: {hashrate/1e3:.0f} H/s | رم: {miner_status['memory_usage_mb']} MB")
                        return
                except:
                    continue
                    
    except Exception as e:
        print(f"⚠️ خطا در دریافت آمار: {e}")

async def periodic_fetch():
    while True:
        await fetch_stats()
        await asyncio.sleep(5)

# ═══════════════════════════════════════════════════════════════
# 🚀 رویدادهای شروع و پایان
# ═══════════════════════════════════════════════════════════════
@app.on_event("startup")
async def startup():
    signal.signal(signal.SIGTERM, lambda sig, frame: None)
    
    # تست استخرها در پس‌زمینه
    asyncio.create_task(test_and_select_best_pool())
    asyncio.create_task(periodic_fetch())
    
    print("🚀 داشبورد ماینینگ راه‌اندازی شد")
    print("📌 برای شروع، آدرس کیف پول مونرو خود را وارد کنید")

@app.on_event("shutdown")
async def shutdown():
    stop_miner()

# ═══════════════════════════════════════════════════════════════
# 🔌 API Endpointها
# ═══════════════════════════════════════════════════════════════
@app.post("/api/start-mining")
async def start_mining(config: WalletConfig):
    if not config.wallet or len(config.wallet.strip()) < 5:
        raise HTTPException(status_code=400, detail="لطفاً آدرس کیف پول را وارد کنید")
    
    if len(config.wallet.strip()) < 90:
        return {
            "status": "warning", 
            "message": "⚠️ این آدرس کوتاه‌تر از آدرس استاندارد مونرو است."
        }
    
    try:
        start_miner(config.wallet)
        return {"status": "ok", "message": f"✅ ماینینگ با {config.wallet[:10]}... شروع شد"}
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

@app.get("/api/pool-test")
async def get_pool_test_results():
    return JSONResponse(miner_status.get("pool_test_results", []))

@app.get("/api/best-pool")
async def get_best_pool():
    return JSONResponse(miner_status.get("best_pool", None))

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "miner_running": miner_status["running"],
        "connected": miner_status["connected"],
        "uptime": miner_status["uptime"],
        "memory_mb": miner_status["memory_usage_mb"],
        "pool": miner_status.get("pool_name", "N/A")
    }

# ═══════════════════════════════════════════════════════════════
# 🖥️ صفحه HTML داشبورد
# ═══════════════════════════════════════════════════════════════
HTML_PAGE = """
<!DOCTYPE html>
<html lang="fa" dir="rtl">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>⛏️ ماینینگ مونرو</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: 'Segoe UI', sans-serif; background: #0a0e1a; color: #e0e8f0; padding: 16px; direction: rtl; }
        .container { max-width: 1100px; margin: auto; }
        .header { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px; border-bottom: 1px solid #1e2a45; padding-bottom: 14px; margin-bottom: 16px; }
        .header h1 { color: #4fc3f7; font-size: 22px; }
        .card { background: #12182b; border: 1px solid #1e2a45; border-radius: 10px; padding: 14px 16px; margin-bottom: 14px; }
        .card-title { font-size: 13px; color: #6a7fa0; margin-bottom: 8px; font-weight: 600; }
        .wallet-section { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
        .wallet-section input { flex: 1; min-width: 200px; padding: 8px 12px; border-radius: 6px; border: 1px solid #1e2a45; background: rgba(255,255,255,0.04); color: #fff; font-size: 13px; }
        .btn { padding: 8px 16px; border-radius: 6px; border: none; font-weight: 600; cursor: pointer; font-size: 13px; }
        .btn-start { background: #1b8a3b; color: #fff; }
        .btn-stop { background: #b71c1c; color: #fff; }
        .btn-refresh { background: rgba(79,195,247,0.12); color: #4fc3f7; border: 1px solid rgba(79,195,247,0.15); }
        .metrics { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; margin-bottom: 14px; }
        .metric { background: #12182b; border: 1px solid #1e2a45; border-radius: 10px; padding: 12px 14px; }
        .metric-label { font-size: 10px; color: #6a7fa0; }
        .metric-value { font-size: 20px; font-weight: 700; margin-top: 2px; }
        .metric-value .unit { font-size: 13px; font-weight: 400; color: #6a7fa0; }
        .chart-container { background: #12182b; border: 1px solid #1e2a45; border-radius: 10px; padding: 14px; margin-top: 10px; }
        .chart-container canvas { width: 100% !important; height: 220px !important; }
        .error-box { background: rgba(239,83,80,0.08); border: 1px solid rgba(239,83,80,0.2); border-radius: 6px; padding: 8px 12px; margin-top: 8px; color: #ef5350; display: none; font-size: 13px; }
        .error-box.show { display: flex; align-items: center; gap: 6px; }
        .footer { text-align: center; color: #6a7fa0; font-size: 11px; padding-top: 14px; border-top: 1px solid #1e2a45; margin-top: 14px; }
        .status-badge { display: inline-flex; align-items: center; gap: 5px; padding: 3px 10px; border-radius: 16px; font-size: 12px; font-weight: 600; }
        .status-badge.online { background: rgba(76,175,80,0.15); color: #4caf50; border: 1px solid rgba(76,175,80,0.3); }
        .status-badge.offline { background: rgba(239,83,80,0.15); color: #ef5350; border: 1px solid rgba(239,83,80,0.3); }
        .status-badge.connecting { background: rgba(255,193,7,0.15); color: #ffc107; border: 1px solid rgba(255,193,7,0.3); }
        .status-badge.best { background: rgba(79,195,247,0.15); color: #4fc3f7; border: 1px solid rgba(79,195,247,0.3); }
        .dot { width: 7px; height: 7px; border-radius: 50%; display: inline-block; }
        .dot.online { background: #4caf50; animation: pulse 2s infinite; }
        .dot.offline { background: #ef5350; }
        .dot.connecting { background: #ffc107; animation: pulse 1s infinite; }
        .dot.best { background: #4fc3f7; }
        @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.3; } }
        .ram-bar { width: 100%; height: 4px; background: #1e2a45; border-radius: 2px; margin-top: 4px; overflow: hidden; }
        .ram-fill { height: 100%; border-radius: 2px; transition: width 0.5s; }
        .pool-item { display: flex; justify-content: space-between; padding: 4px 0; border-bottom: 1px solid #1a1f35; font-size: 12px; }
        .pool-item:last-child { border-bottom: none; }
        .pool-status { font-weight: 600; }
        .pool-status.ok { color: #4caf50; }
        .pool-status.fail { color: #ef5350; }
        .pool-status.best { color: #4fc3f7; }
        .best-pool-badge { background: rgba(79,195,247,0.15); color: #4fc3f7; padding: 2px 8px; border-radius: 12px; font-size: 10px; }
        @media (max-width: 500px) { .header h1 { font-size: 17px; } .metric-value { font-size: 17px; } }
    </style>
</head>
<body>
<div class="container">
    <div class="header">
        <h1>⛏️ مونرو</h1>
        <div>
            <span class="status-badge" id="statusBadge"><span class="dot offline" id="statusDot"></span><span id="statusText">غیرفعال</span></span>
            <button class="btn btn-refresh" onclick="fetchAll()">🔄</button>
        </div>
    </div>
    
    <div class="card">
        <div class="wallet-section">
            <input type="text" id="walletInput" placeholder="آدرس مونرو (95 کاراکتر)" value="48edfHu7V9Z84YzzMa6fUueoELZ9ZRXq9VetWzYGzKt52XU5xvqgzYnDK9URnRoJMk1j8nLwEVsaSWJ4fhdUyZijBGUicoD">
            <button class="btn btn-start" onclick="startMining()">▶ شروع</button>
            <button class="btn btn-stop" onclick="stopMining()">⏹ توقف</button>
        </div>
        <div id="statusMsg" style="margin-top:6px;font-size:12px;color:#6a7fa0;"></div>
        <div class="error-box" id="errorBox"><span id="errorText"></span></div>
    </div>

    <div class="metrics">
        <div class="metric"><div class="metric-label">هش‌ریت</div><div class="metric-value" id="hashrate">--</div></div>
        <div class="metric"><div class="metric-label">شار خوب</div><div class="metric-value" id="sharesGood">--</div></div>
        <div class="metric"><div class="metric-label">آپتایم</div><div class="metric-value" id="uptime">--</div></div>
        <div class="metric" style="border-color: rgba(79,195,247,0.2);">
            <div class="metric-label">🧠 مصرف رم</div>
            <div class="metric-value" id="ramUsage">-- <span class="unit">MB</span></div>
            <div class="ram-bar"><div class="ram-fill" id="ramFill" style="width:0%;background:#4fc3f7;"></div></div>
        </div>
    </div>

    <div class="card">
        <div class="card-title">🏆 بهترین استخر <span id="bestPoolName" style="color:#4fc3f7;font-weight:700;"></span></div>
        <div id="bestPoolInfo" style="font-size:13px;color:#6a7fa0;">در حال تست...</div>
    </div>

    <div class="card">
        <div class="card-title">🌐 نتایج تست استخرها</div>
        <div id="poolTestResults"><span style="color:#6a7fa0;">⏳ در حال تست...</span></div>
    </div>

    <div class="chart-container"><canvas id="chart"></canvas></div>
    <div class="footer">⚡ رم &lt; 512MB · <a href="https://t.me/CodeBoxo" target="_blank">@CodeBoxo</a></div>
</div>
<script>
let chartInstance = null, historyData = [];

async function startMining() {
    const wallet = document.getElementById('walletInput').value.trim();
    if (!wallet || wallet.length < 5) { alert('آدرس را وارد کنید'); return; }
    document.getElementById('statusMsg').innerHTML = '🔄 راه‌اندازی...';
    document.getElementById('errorBox').classList.remove('show');
    try {
        const res = await fetch('/api/start-mining', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ wallet }) });
        const data = await res.json();
        if (res.ok) { document.getElementById('statusMsg').innerHTML = '✅ ' + data.message; }
        else { document.getElementById('statusMsg').innerHTML = '❌ ' + data.detail; showError(data.detail); }
    } catch(e) { document.getElementById('statusMsg').innerHTML = '❌ خطا'; showError(e.message); }
    fetchAll();
}

async function stopMining() {
    document.getElementById('statusMsg').innerHTML = '⏹ توقف...';
    try { const res = await fetch('/api/stop-mining', { method: 'POST' }); const data = await res.json(); document.getElementById('statusMsg').innerHTML = '✅ ' + data.message; } catch(e) { document.getElementById('statusMsg').innerHTML = '❌ خطا'; }
    fetchAll();
}

async function fetchStatus() {
    try { const res = await fetch('/api/miner-status'); const data = await res.json(); updateUI(data); } catch(e) { console.error(e); }
}

async function fetchHistory() {
    try { const res = await fetch('/api/history'); historyData = await res.json(); updateChart(); } catch(e) { console.error(e); }
}

async function fetchPoolTestResults() {
    try {
        const res = await fetch('/api/pool-test');
        const data = await res.json();
        const container = document.getElementById('poolTestResults');
        if (!data || data.length === 0) { container.innerHTML = '<span style="color:#6a7fa0;">⏳ در حال تست...</span>'; return; }
        let html = '';
        let workingCount = 0;
        for (const pool of data) {
            if (pool.working) workingCount++;
            const status = pool.working ? '✅' : '❌';
            const cls = pool.working ? 'ok' : 'fail';
            const portInfo = pool.working ? `پورت: ${pool.best_port} (${pool.response_time?.toFixed(2) || '?'}s)` : 'بدون پاسخ';
            const bestTag = pool.working && pool === data.find(p => p.working && p.name === document.getElementById('bestPoolName')?.textContent?.trim()) ? ' <span class="best-pool-badge">🏆 بهترین</span>' : '';
            html += `<div class="pool-item"><span>${status} <strong>${pool.name}</strong> (${pool.url})</span><span class="pool-status ${cls}">${portInfo}${bestTag}</span></div>`;
        }
        container.innerHTML = html;
        const best = document.getElementById('bestPoolName');
        const bestInfo = document.getElementById('bestPoolInfo');
        try {
            const res2 = await fetch('/api/best-pool');
            const bestData = await res2.json();
            if (bestData && bestData.name) {
                best.textContent = `${bestData.name} (${bestData.url}:${bestData.port})`;
                bestInfo.textContent = `⏱️ زمان پاسخ: ${bestData.response_time?.toFixed(2) || '?'} ثانیه`;
            } else {
                best.textContent = 'هیچ استخری در دسترس نیست';
                bestInfo.textContent = 'لطفاً اتصال اینترنت را بررسی کنید';
            }
        } catch(e) { best.textContent = 'خطا در دریافت'; }
    } catch(e) { console.error(e); }
}

function updateUI(data) {
    const hr = data.hashrate || 0;
    document.getElementById('hashrate').textContent = hr > 0 ? (hr/1e3).toFixed(1) + ' KH/s' : '--';
    document.getElementById('sharesGood').textContent = data.shares_good || 0;
    document.getElementById('uptime').textContent = data.running ? formatUptime(data.uptime) : '--';
    
    const ram = data.memory_usage_mb || 0;
    document.getElementById('ramUsage').innerHTML = ram > 0 ? ram + ' <span class="unit">MB</span>' : '-- <span class="unit">MB</span>';
    const pct = Math.min(100, (ram / 512) * 100);
    const fill = document.getElementById('ramFill');
    fill.style.width = pct + '%';
    if (pct > 80) { fill.style.background = '#ef5350'; } 
    else if (pct > 60) { fill.style.background = '#ffc107'; } 
    else { fill.style.background = '#4fc3f7'; }
    
    const badge = document.getElementById('statusBadge'), dot = document.getElementById('statusDot'), text = document.getElementById('statusText');
    if (data.running && data.connected) { badge.className = 'status-badge online'; dot.className = 'dot online'; text.textContent = '⛏️ فعال'; }
    else if (data.running) { badge.className = 'status-badge connecting'; dot.className = 'dot connecting'; text.textContent = '🔄 اتصال...'; }
    else { badge.className = 'status-badge offline'; dot.className = 'dot offline'; text.textContent = '⏹️ غیرفعال'; }
    
    if (data.error) { showError(data.error); } else { document.getElementById('errorBox').classList.remove('show'); }
    if (data.running && data.hashrate > 0) { historyData.push({ time: new Date().toISOString(), hashrate: data.hashrate }); if (historyData.length > 100) historyData.shift(); updateChart(); }
}

function showError(msg) { const box = document.getElementById('errorBox'); document.getElementById('errorText').textContent = msg; box.classList.add('show'); }
function formatUptime(sec) { const h = Math.floor(sec/3600), m = Math.floor((sec%3600)/60), s = sec%60; return h+'h '+m+'m '+s+'s'; }

function updateChart() {
    const labels = historyData.map(p => new Date(p.time).toLocaleTimeString('fa-IR')), values = historyData.map(p => p.hashrate/1e3);
    const ctx = document.getElementById('chart').getContext('2d');
    if (chartInstance) { chartInstance.data.labels = labels; chartInstance.data.datasets[0].data = values; chartInstance.update('none'); }
    else { chartInstance = new Chart(ctx, { type: 'line', data: { labels, datasets: [{ label: 'هش‌ریت (KH/s)', data: values, borderColor: '#4fc3f7', backgroundColor: 'rgba(79,195,247,0.08)', fill: true, tension: 0.4, pointRadius: 2, borderWidth: 2 }] }, options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { labels: { color: '#6a7fa0' } } }, scales: { x: { ticks: { color: '#6a7fa0', maxTicksLimit: 12 } }, y: { ticks: { color: '#6a7fa0' }, beginAtZero: true } } } }); }
}

async function fetchAll() {
    await fetchStatus();
    await fetchHistory();
    await fetchPoolTestResults();
}
fetchAll();
setInterval(fetchAll, 5000);
</script>
</body></html>
"""

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return HTMLResponse(HTML_PAGE)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
