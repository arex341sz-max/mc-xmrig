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
    "connecting": False,
    "best_pool": None,
    "demo_mode": False,
}
history = []
pool_test_results = []

# ─── لیست استخرها ──────────────────────────────────────────────────────────────
MINING_POOLS = [
    {"name": "SupportXMR", "url": "pool.supportxmr.com", "port": 443, "tls": True},
    {"name": "MoneroOcean", "url": "gulf.moneroocean.stream", "port": 10128, "tls": True},
    {"name": "Nanopool", "url": "xmr.nanopool.org", "port": 14433, "tls": True},
    {"name": "HashVault", "url": "pool.hashvault.pro", "port": 443, "tls": True},
    {"name": "OMINE", "url": "xmr.omine.ga", "port": 3000, "tls": True},
]

# ─── تست اتصال به استخر ──────────────────────────────────────────────────────
async def test_single_pool(pool: dict) -> dict:
    start = time.time()
    result = {
        "name": pool["name"],
        "url": pool["url"],
        "port": pool["port"],
        "working": False,
        "response_time": 999,
        "error": None
    }
    try:
        if pool.get("tls", False):
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            with socket.create_connection((pool["url"], pool["port"]), timeout=3) as sock:
                with context.wrap_socket(sock, server_hostname=pool["url"]) as ssock:
                    result["working"] = True
        else:
            with socket.create_connection((pool["url"], pool["port"]), timeout=3) as sock:
                result["working"] = True
        result["response_time"] = round((time.time() - start) * 1000, 0)
    except Exception as e:
        result["error"] = str(e)
    return result

async def test_all_pools():
    global pool_test_results, miner_status
    print("🔍 در حال تست استخرها...")
    results = []
    for pool in MINING_POOLS:
        result = await test_single_pool(pool)
        results.append(result)
        if result["working"]:
            print(f"    ✅ {pool['name']} کار می‌کند! ({result['response_time']}ms)")
        else:
            print(f"    ❌ {pool['name']} پاسخ نمی‌دهد")
        await asyncio.sleep(0.3)
    pool_test_results = results
    working_pools = [p for p in results if p["working"]]
    if working_pools:
        best = min(working_pools, key=lambda x: x["response_time"])
        miner_status["best_pool"] = best
        print(f"🏆 بهترین استخر: {best['name']} ({best['response_time']}ms)")
        return best
    else:
        print("⚠️ هیچ استخری پاسخ نداد! استفاده از SupportXMR به عنوان پیش‌فرض")
        return {"name": "SupportXMR", "url": "pool.supportxmr.com", "port": 443, "tls": True}

# ─── توابع مدیریت ماینر ──────────────────────────────────────────────────────
def get_process_memory():
    if miner_process and miner_process.pid:
        try:
            proc = psutil.Process(miner_process.pid)
            return round(proc.memory_info().rss / 1024 / 1024, 1)
        except:
            return 0
    return 0

def get_system_memory():
    """دریافت حافظه کل سیستم و حافظه آزاد"""
    try:
        mem = psutil.virtual_memory()
        return {
            "total_mb": round(mem.total / 1024 / 1024, 1),
            "available_mb": round(mem.available / 1024 / 1024, 1),
            "used_mb": round(mem.used / 1024 / 1024, 1),
            "percent": mem.percent
        }
    except:
        return {"total_mb": 512, "available_mb": 200, "used_mb": 312, "percent": 61}

def generate_config(wallet_address: str, pool_url: str, pool_port: int, use_tls: bool, demo_mode: bool = False) -> str:
    """تولید کانفیگ - در حالت دمو، هیچ هسته‌ای استفاده نمی‌شود"""
    threads = 0 if demo_mode else 0.5  # در حالت دمو، ۰ هسته
    
    template = {
        "autosave": False,
        "cpu": {
            "enabled": True if not demo_mode else False,
            "huge-pages": False,
            "hw-aes": True,
            "max-threads-hint": threads,
            "asm": True,
            "priority": 5,
            "mode": "light"
        },
        "pools": [
            {
                "url": f"{pool_url}:{pool_port}",
                "user": wallet_address,
                "pass": "railway_worker",
                "tls": use_tls,
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
    return config_path

def start_demo_miner(wallet: str):
    """راه‌اندازی ماینر در حالت دمو (بدون استخراج واقعی)"""
    global miner_process, miner_status
    
    print("🎭 راه‌اندازی ماینر در حالت دمو (بدون استخراج واقعی)")
    
    miner_status["running"] = True
    miner_status["demo_mode"] = True
    miner_status["wallet"] = wallet
    miner_status["start_time"] = time.time()
    miner_status["connected"] = True
    miner_status["error"] = None
    miner_status["pool_name"] = "حالت دمو"
    miner_status["pool"] = "demo-mode"
    
    # در حالت دمو، یک ترد مجازی برای شبیه‌سازی آمار
    asyncio.create_task(demo_miner_loop())
    print("✅ ماینر دمو راه‌اندازی شد")

async def demo_miner_loop():
    """شبیه‌سازی آمار ماینر در حالت دمو"""
    global miner_status, history
    start_time = time.time()
    share_count = 0
    
    while miner_status["running"] and miner_status["demo_mode"]:
        # شبیه‌سازی هش‌ریت تصادفی (۵۰-۲۰۰ H/s)
        hashrate = 50 + (hash(time.time()) % 150)
        miner_status["hashrate"] = hashrate
        if hashrate > miner_status["hashrate_highest"]:
            miner_status["hashrate_highest"] = hashrate
        
        # هر ۳۰ ثانیه یک شار شبیه‌سازی
        share_count += 1
        if share_count % 3 == 0:
            miner_status["shares_good"] += 1
        
        miner_status["uptime"] = int(time.time() - start_time)
        miner_status["last_update"] = time.time()
        miner_status["memory_usage_mb"] = get_process_memory() or 45
        
        history.append({
            "time": datetime.now().isoformat(),
            "hashrate": hashrate
        })
        if len(history) > 100:
            history = history[-100:]
        
        await asyncio.sleep(2)

async def start_miner_with_timeout(wallet: str, timeout_seconds: int = 30):
    """ماینر را با تایم‌اوت اجرا می‌کند - اگر رم کافی نباشد، حالت دمو فعال می‌شود"""
    global miner_process, miner_status
    
    # بررسی حافظه سیستم
    mem_info = get_system_memory()
    available_ram = mem_info["available_mb"]
    
    print(f"📊 حافظه موجود: {available_ram} MB / {mem_info['total_mb']} MB")
    
    # اگر رم کمتر از ۴۰۰ مگابایت باشد، حالت دمو فعال می‌شود
    if available_ram < 400:
        print("⚠️ رم کافی برای استخراج واقعی وجود ندارد! فعال‌سازی حالت دمو...")
        miner_status["error"] = "حالت دمو (رم کافی نیست)"
        start_demo_miner(wallet)
        return True
    
    # در غیر این صورت، استخراج واقعی
    best = miner_status.get("best_pool")
    if not best:
        best = await test_all_pools()
    
    if not best or not best.get("working"):
        best = {"name": "SupportXMR", "url": "pool.supportxmr.com", "port": 443, "tls": True}
    
    pool_url = best["url"]
    pool_port = best["port"]
    use_tls = best.get("tls", True)
    pool_name = best["name"]
    
    print(f"🌐 استفاده از استخر: {pool_name} ({pool_url}:{pool_port})")
    
    if miner_process and miner_process.poll() is None:
        miner_process.terminate()
        time.sleep(2)
        if miner_process.poll() is None:
            miner_process.kill()
        miner_process = None

    config_path = generate_config(wallet, pool_url, pool_port, use_tls, demo_mode=False)

    xmrig_path = "/usr/local/bin/xmrig"
    if not os.path.exists(xmrig_path):
        xmrig_path = "/xmrig/build/xmrig"
        if not os.path.exists(xmrig_path):
            miner_status["error"] = "xmrig not found! فعال‌سازی حالت دمو..."
            start_demo_miner(wallet)
            return True

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
        miner_status["demo_mode"] = False
        miner_status["connecting"] = True
        miner_status["wallet"] = wallet
        miner_status["start_time"] = time.time()
        miner_status["error"] = None
        miner_status["hashrate"] = 0
        miner_status["shares_good"] = 0
        miner_status["connected"] = False
        miner_status["pool"] = f"{pool_url}:{pool_port}"
        miner_status["pool_name"] = pool_name
        
        print(f"✅ ماینر با کیف پول {wallet[:8]}... راه‌اندازی شد (PID: {miner_process.pid})")
        
        asyncio.create_task(wait_for_api_with_timeout(timeout_seconds))
        asyncio.create_task(monitor_miner())
        return True
        
    except Exception as e:
        print(f"❌ خطا در راه‌اندازی ماینر: {e}")
        print("🎭 فعال‌سازی حالت دمو...")
        start_demo_miner(wallet)
        return True

async def wait_for_api_with_timeout(timeout: int = 30):
    for i in range(timeout):
        if not miner_status["running"] or miner_status["demo_mode"]:
            return
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                resp = await client.get("http://localhost:8081/api/summary")
                if resp.status_code == 200:
                    miner_status["connected"] = True
                    miner_status["connecting"] = False
                    print("✅ API ماینر فعال شد و به استخر متصل گردید")
                    return
        except:
            pass
        await asyncio.sleep(1)
    
    # اگر timeout شد و هنوز وصل نشده، به حالت دمو برو
    print("⏰ زمان اتصال به استخر به پایان رسید. فعال‌سازی حالت دمو...")
    wallet = miner_status.get("wallet", "")
    stop_miner()
    start_demo_miner(wallet)

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
                    miner_status["connected"] = True
                    miner_status["connecting"] = False
                elif "reject" in line.lower():
                    miner_status["shares_rejected"] += 1
                elif "error" in line.lower() or "failed" in line.lower():
                    miner_status["error"] = line
                elif "connected" in line.lower():
                    miner_status["connected"] = True
                    miner_status["connecting"] = False
                    
            miner_status["memory_usage_mb"] = get_process_memory()
            await asyncio.sleep(0.1)
        
        if miner_process and miner_process.poll() is not None:
            exit_code = miner_process.poll()
            print(f"⚠️ ماینر با کد {exit_code} متوقف شد")
            # اگر ماینر با خطا متوقف شد، به حالت دمو برو
            if exit_code != 0:
                wallet = miner_status.get("wallet", "")
                stop_miner()
                start_demo_miner(wallet)
            
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
    miner_status["connecting"] = False
    miner_status["demo_mode"] = False
    miner_status["memory_usage_mb"] = 0
    print("⏹️ ماینر متوقف شد")

# ─── دریافت آمار ──────────────────────────────────────────────────────────────
async def fetch_stats():
    global miner_status, history
    
    if not miner_status["running"]:
        return
    
    # در حالت دمو، نیازی به دریافت از API نیست
    if miner_status["demo_mode"]:
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
                        history.append({"time": datetime.now().isoformat(), "hashrate": hashrate})
                        if len(history) > 100:
                            history = history[-100:]
                        return
                except:
                    continue
    except Exception as e:
        print(f"⚠️ خطا در دریافت آمار: {e}")

async def periodic_fetch():
    while True:
        await fetch_stats()
        await asyncio.sleep(5)

# ─── رویدادهای شروع و پایان ──────────────────────────────────────────────────
@app.on_event("startup")
async def startup():
    signal.signal(signal.SIGTERM, lambda sig, frame: None)
    asyncio.create_task(periodic_fetch())
    asyncio.create_task(test_all_pools())
    print("🚀 داشبورد ماینینگ راه‌اندازی شد")
    print("💡 در صورت کمبود رم، حالت دمو به‌طور خودکار فعال می‌شود")

@app.on_event("shutdown")
async def shutdown():
    stop_miner()

# ─── API ──────────────────────────────────────────────────────────────────────
@app.get("/api/pool-test")
async def get_pool_test():
    return JSONResponse(pool_test_results)

@app.get("/api/best-pool")
async def get_best_pool():
    return JSONResponse(miner_status.get("best_pool", {}))

@app.post("/api/start-mining")
async def start_mining(config: WalletConfig):
    if not config.wallet or len(config.wallet.strip()) < 5:
        raise HTTPException(status_code=400, detail="لطفاً آدرس کیف پول را وارد کنید")
    
    if len(config.wallet.strip()) < 90:
        return {"status": "warning", "message": "⚠️ این آدرس کوتاه‌تر از آدرس استاندارد مونرو است."}
    
    try:
        await start_miner_with_timeout(config.wallet, timeout_seconds=30)
        mode = "دمو" if miner_status.get("demo_mode") else "واقعی"
        return {"status": "ok", "message": f"✅ ماینینگ با {config.wallet[:10]}... شروع شد (حالت {mode})"}
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

@app.get("/api/system-memory")
async def get_system_memory():
    return JSONResponse(get_system_memory())

@app.get("/health")
async def health():
    mem = get_system_memory()
    return {
        "status": "ok",
        "miner_running": miner_status["running"],
        "demo_mode": miner_status["demo_mode"],
        "connected": miner_status["connected"],
        "uptime": miner_status["uptime"],
        "memory_mb": miner_status["memory_usage_mb"],
        "system_ram_available_mb": mem["available_mb"],
        "system_ram_percent": mem["percent"]
    }

# ─── صفحه HTML ─────────────────────────────────────────────────────────────────
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
        .metric-value .demo-badge { font-size: 10px; background: #ffc107; color: #000; padding: 2px 8px; border-radius: 12px; font-weight: 600; }
        .chart-container { background: #12182b; border: 1px solid #1e2a45; border-radius: 10px; padding: 14px; margin-top: 10px; }
        .chart-container canvas { width: 100% !important; height: 220px !important; }
        .error-box { background: rgba(239,83,80,0.08); border: 1px solid rgba(239,83,80,0.2); border-radius: 6px; padding: 8px 12px; margin-top: 8px; color: #ef5350; display: none; font-size: 13px; }
        .error-box.show { display: flex; align-items: center; gap: 6px; }
        .footer { text-align: center; color: #6a7fa0; font-size: 11px; padding-top: 14px; border-top: 1px solid #1e2a45; margin-top: 14px; }
        .status-badge { display: inline-flex; align-items: center; gap: 5px; padding: 3px 10px; border-radius: 16px; font-size: 12px; font-weight: 600; }
        .status-badge.online { background: rgba(76,175,80,0.15); color: #4caf50; border: 1px solid rgba(76,175,80,0.3); }
        .status-badge.offline { background: rgba(239,83,80,0.15); color: #ef5350; border: 1px solid rgba(239,83,80,0.3); }
        .status-badge.connecting { background: rgba(255,193,7,0.15); color: #ffc107; border: 1px solid rgba(255,193,7,0.3); }
        .status-badge.demo { background: rgba(156,39,176,0.15); color: #ce93d8; border: 1px solid rgba(156,39,176,0.3); }
        .dot { width: 7px; height: 7px; border-radius: 50%; display: inline-block; }
        .dot.online { background: #4caf50; animation: pulse 2s infinite; }
        .dot.offline { background: #ef5350; }
        .dot.connecting { background: #ffc107; animation: pulse 1s infinite; }
        .dot.demo { background: #ce93d8; animation: pulse 2s infinite; }
        @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.3; } }
        .ram-bar { width: 100%; height: 4px; background: #1e2a45; border-radius: 2px; margin-top: 4px; overflow: hidden; }
        .ram-fill { height: 100%; border-radius: 2px; transition: width 0.5s; }
        .pool-list { font-size: 12px; }
        .pool-list .ok { color: #4caf50; }
        .pool-list .fail { color: #ef5350; }
        .system-ram { font-size: 11px; color: #6a7fa0; margin-top: 4px; }
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
        <div class="metric"><div class="metric-label">هش‌ریت</div><div class="metric-value" id="hashrate">-- <span id="demoBadge"></span></div></div>
        <div class="metric"><div class="metric-label">شار خوب</div><div class="metric-value" id="sharesGood">--</div></div>
        <div class="metric"><div class="metric-label">آپتایم</div><div class="metric-value" id="uptime">--</div></div>
        <div class="metric" style="border-color: rgba(79,195,247,0.2);">
            <div class="metric-label">🧠 مصرف رم</div>
            <div class="metric-value" id="ramUsage">-- <span class="unit">MB</span></div>
            <div class="ram-bar"><div class="ram-fill" id="ramFill" style="width:0%;background:#4fc3f7;"></div></div>
            <div class="system-ram" id="systemRam"></div>
        </div>
    </div>
    <div class="card">
        <div class="metric-label">🌐 نتایج تست استخرها</div>
        <div id="poolTestResults" class="pool-list" style="margin-top:6px;font-size:12px;">
            <span style="color:#6a7fa0;">در حال تست...</span>
        </div>
        <div id="bestPoolDisplay" style="margin-top:6px;font-size:12px;color:#4fc3f7;"></div>
    </div>
    <div class="chart-container"><canvas id="chart"></canvas></div>
    <div class="footer">⚡ رم &lt; 512MB · حالت دمو در صورت کمبود رم · <a href="https://t.me/CodeBoxo" target="_blank">@CodeBoxo</a></div>
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
async function fetchPoolResults() {
    try {
        const res = await fetch('/api/pool-test');
        const data = await res.json();
        const container = document.getElementById('poolTestResults');
        if (!data || data.length === 0) { container.innerHTML = '<span style="color:#6a7fa0;">⏳ در حال تست...</span>'; return; }
        let html = '';
        for (const p of data) {
            const icon = p.working ? '✅' : '❌';
            const color = p.working ? '#4caf50' : '#ef5350';
            const time = p.working ? p.response_time + 'ms' : '—';
            html += `<div style="display:flex;justify-content:space-between;padding:3px 0;border-bottom:1px solid #1e2a45;">
                <span>${icon} <strong>${p.name}</strong> (${p.url}:${p.port})</span>
                <span style="color:${color};font-size:11px;">${time}</span>
            </div>`;
        }
        container.innerHTML = html;
    } catch(e) { console.error(e); }
}
async function fetchBestPool() {
    try {
        const res = await fetch('/api/best-pool');
        const data = await res.json();
        const el = document.getElementById('bestPoolDisplay');
        if (data && data.working) {
            el.innerHTML = `🏆 بهترین استخر: <strong>${data.name}</strong> (${data.response_time}ms) — ${data.url}:${data.port}`;
        } else {
            el.innerHTML = '⚠️ هیچ استخری در دسترس نیست';
        }
    } catch(e) { console.error(e); }
}
async function fetchSystemMemory() {
    try {
        const res = await fetch('/api/system-memory');
        const data = await res.json();
        document.getElementById('systemRam').textContent = 
            `حافظه کل: ${data.total_mb}MB | موجود: ${data.available_mb}MB | استفاده: ${data.percent}%`;
    } catch(e) { console.error(e); }
}
function updateUI(data) {
    const hr = data.hashrate || 0;
    const isDemo = data.demo_mode || false;
    const badge = document.getElementById('demoBadge');
    if (isDemo) {
        badge.innerHTML = ' <span class="demo-badge">🎭 دمو</span>';
    } else {
        badge.innerHTML = '';
    }
    document.getElementById('hashrate').innerHTML = (hr > 0 ? (hr/1e3).toFixed(1) + ' KH/s' : '--') + ' <span id="demoBadge"></span>';
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
    
    const badgeEl = document.getElementById('statusBadge'), dot = document.getElementById('statusDot'), text = document.getElementById('statusText');
    if (data.demo_mode) {
        badgeEl.className = 'status-badge demo';
        dot.className = 'dot demo';
        text.textContent = '🎭 دمو';
    } else if (data.running && data.connected) {
        badgeEl.className = 'status-badge online';
        dot.className = 'dot online';
        text.textContent = '⛏️ فعال';
    } else if (data.running && data.connecting) {
        badgeEl.className = 'status-badge connecting';
        dot.className = 'dot connecting';
        text.textContent = '🔄 اتصال...';
    } else if (data.running) {
        badgeEl.className = 'status-badge connecting';
        dot.className = 'dot connecting';
        text.textContent = '🔄 در حال تلاش...';
    } else {
        badgeEl.className = 'status-badge offline';
        dot.className = 'dot offline';
        text.textContent = '⏹️ غیرفعال';
    }
    
    if (data.error && !data.demo_mode) { showError(data.error); } else { document.getElementById('errorBox').classList.remove('show'); }
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
async function fetchAll() { await fetchStatus(); await fetchHistory(); await fetchPoolResults(); await fetchBestPool(); await fetchSystemMemory(); }
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
