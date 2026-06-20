import os
import json
import subprocess
import time
import asyncio
import httpx
import signal
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
    "shares": 0,
    "uptime": 0,
    "last_update": None,
    "error": None,
}

def generate_config(wallet_address: str) -> str:
    """فایل config.json با تنظیمات API دقیق"""
    template = {
        "autosave": False,
        "cpu": {
            "enabled": True,
            "huge-pages": False,
            "hw-aes": True,
            "max-threads-hint": 2,
            "asm": True,
            "priority": 5
        },
        "pools": [
            {
                "url": "pool.supportxmr.com:443",
                "user": wallet_address,
                "pass": "worker",
                "tls": True,
                "keepalive": True
            }
        ],
        "api": {
            "port": 8080,
            "access-token": None,
            "worker-id": "railway",
            "ipv6": False
        },
        "donate-level": 1,
        "opencl": False,
        "cuda": False,
        "print-time": 60,
        "retries": 999,
        "retry-pause": 10,
        "health-print-time": 60,
        "http": {
            "enabled": True,
            "port": 8080,
            "access-token": None,
            "restricted": True
        }
    }
    config_path = "/app/config.json"
    with open(config_path, "w") as f:
        json.dump(template, f, indent=2)
    return config_path

def start_miner(wallet: str):
    global miner_process, miner_status
    
    if miner_process and miner_process.poll() is None:
        miner_process.terminate()
        time.sleep(2)
        if miner_process.poll() is None:
            miner_process.kill()
        miner_process = None

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
        # اجرا با تنظیمات لاگ کامل
        miner_process = subprocess.Popen(
            [xmrig_path, "-c", config_path, "--donate-level=1", "--verbose"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1
        )
        
        miner_status["running"] = True
        miner_status["wallet"] = wallet
        miner_status["start_time"] = time.time()
        miner_status["error"] = None
        print(f"✅ ماینر با کیف پول {wallet[:8]}... راه‌اندازی شد (PID: {miner_process.pid})")
        
        # صبر برای راه‌اندازی کامل API
        asyncio.create_task(wait_for_api())
        asyncio.create_task(monitor_miner())
        
    except Exception as e:
        miner_status["running"] = False
        miner_status["error"] = str(e)
        print(f"❌ خطا: {e}")
        raise

async def wait_for_api():
    """منتظر می‌ماند تا API ماینر بالا بیاید"""
    for i in range(30):  # حداکثر 30 ثانیه
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                resp = await client.get("http://localhost:8080/api/summary")
                if resp.status_code == 200:
                    print("✅ API ماینر فعال شد")
                    return
        except:
            pass
        await asyncio.sleep(1)
    print("⚠️ API ماینر پس از 30 ثانیه فعال نشد")

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
                if "error" in line.lower() or "failed" in line.lower() or "reject" in line.lower():
                    miner_status["error"] = line
            await asyncio.sleep(0.1)
        
        if miner_process and miner_process.poll() is not None:
            exit_code = miner_process.poll()
            print(f"⚠️ ماینر با کد {exit_code} متوقف شد")
            miner_status["running"] = False
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
    print("⏹️ ماینر متوقف شد")

async def fetch_stats():
    global miner_status
    if not miner_status["running"]:
        return
        
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            # امتحان هر دو مسیر ممکن
            for path in ["/api/summary", "/summary", "/2/summary"]:
                try:
                    resp = await client.get(f"http://localhost:8080{path}")
                    if resp.status_code == 200:
                        data = resp.json()
                        miner_status["hashrate"] = data.get("hashrate", {}).get("total", [0])[0]
                        miner_status["shares"] = data.get("results", {}).get("shares_good", 0)
                        miner_status["uptime"] = int(time.time() - miner_status.get("start_time", time.time()))
                        miner_status["last_update"] = time.time()
                        print(f"📊 هش: {miner_status['hashrate']/1e3:.0f} H/s | شار: {miner_status['shares']}")
                        return
                except:
                    continue
            print("⚠️ هیچ مسیر API در دسترس نبود")
    except Exception as e:
        print(f"⚠️ خطا در دریافت آمار: {e}")

async def periodic_fetch():
    while True:
        await fetch_stats()
        await asyncio.sleep(10)

@app.on_event("startup")
async def startup():
    signal.signal(signal.SIGTERM, lambda sig, frame: None)
    asyncio.create_task(periodic_fetch())
    print("🚀 داشبورد راه‌اندازی شد")

@app.on_event("shutdown")
async def shutdown():
    stop_miner()

@app.post("/api/start-mining")
async def start_mining(config: WalletConfig):
    if not config.wallet or len(config.wallet.strip()) < 5:
        raise HTTPException(status_code=400, detail="آدرس کیف پول را وارد کنید")
    try:
        start_miner(config.wallet)
        return {"status": "ok", "message": f"شروع با {config.wallet[:8]}..."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/stop-mining")
async def stop_mining():
    stop_miner()
    return {"status": "ok", "message": "ماینر متوقف شد"}

@app.get("/api/miner-status")
async def get_miner_status():
    return JSONResponse(miner_status)

# صفحه HTML (همان که قبلاً بود - برای اختصار حذف شده، اما کامل در کد نهایی موجود است)
HTML_PAGE = """... (همان HTML قبلی) ..."""

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return HTMLResponse(HTML_PAGE)

@app.get("/health")
async def health():
    return {"status": "ok", "miner": miner_status["running"]}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
