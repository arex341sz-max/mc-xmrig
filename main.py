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

# ─── مدل داده ──────────────────────────────────────────────────────────────────
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
    "error": None,
}

# ─── توابع مدیریت ماینر (با مصرف بهینه رم) ──────────────────────────────────
def generate_config(wallet_address: str) -> str:
    """فایل config.json با تنظیمات کم‌مصرف برای Railway"""
    template = {
        "autosave": False,
        "cpu": {
            "enabled": True,
            "huge-pages": False,        # غیرفعال برای کاهش رم
            "hw-aes": True,
            "max-threads-hint": 2,       # فقط ۲ هسته برای مصرف کم رم
            "asm": True,
            "priority": 5                # اولویت پایین برای جلوگیری از محدودیت
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

    # مسیر فایل اجرایی
    xmrig_path = "/usr/local/bin/xmrig"
    if not os.path.exists(xmrig_path):
        xmrig_path = "/xmrig/build/xmrig"
        if not os.path.exists(xmrig_path):
            miner_status["error"] = "xmrig not found!"
            raise Exception("xmrig executable not found!")

    if not os.access(xmrig_path, os.X_OK):
        os.chmod(xmrig_path, 0o755)

    try:
        # اجرا با تنظیمات کم‌مصرف
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
        print(f"✅ ماینر با کیف پول {wallet[:8]}... راه‌اندازی شد (PID: {miner_process.pid})")
        
        asyncio.create_task(monitor_miner())
        
    except Exception as e:
        miner_status["running"] = False
        miner_status["error"] = str(e)
        print(f"❌ خطا: {e}")
        raise

async def monitor_miner():
    """پایش ماینر و تشخیص خطاها"""
    global miner_process, miner_status
    if not miner_process:
        return
    
    try:
        while miner_process and miner_process.poll() is None:
            line = await asyncio.to_thread(miner_process.stderr.readline)
            if line:
                line = line.strip()
                print(f"[XMRig] {line}")
                if "error" in line.lower() or "failed" in line.lower():
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

# ─── دریافت آمار ──────────────────────────────────────────────────────────────
async def fetch_stats():
    global miner_status
    if not miner_status["running"]:
        return
        
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get("http://localhost:8080/api/summary")
            if resp.status_code == 200:
                data = resp.json()
                miner_status["hashrate"] = data.get("hashrate", {}).get("total", [0])[0]
                miner_status["shares"] = data.get("results", {}).get("shares_good", 0)
                miner_status["uptime"] = int(time.time() - miner_status.get("start_time", time.time()))
                miner_status["last_update"] = time.time()
                print(f"📊 هش: {miner_status['hashrate']/1e3:.0f} H/s | شار: {miner_status['shares']}")
            else:
                print(f"⚠️ API: {resp.status_code}")
    except httpx.ConnectError:
        print("⏳ اتصال به API برقرار نیست...")
    except Exception as e:
        print(f"⚠️ خطا: {e}")

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

# ─── API ──────────────────────────────────────────────────────────────────────
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

# ─── صفحه HTML ────────────────────────────────────────────────────────────────
HTML_PAGE = """<!DOCTYPE html>
<html lang="fa" dir="rtl">
<head>
    <meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>داشبورد ماینینگ</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        *{margin:0;padding:0;box-sizing:border-box}
        body{font-family:'Segoe UI',sans-serif;background:#0a0f1e;color:#e0e8f0;padding:20px;direction:rtl}
        .container{max-width:1200px;margin:auto}
        h1{text-align:center;margin-bottom:20px;color:#4fc3f7}
        .card{background:#141b2b;border-radius:12px;padding:18px;border:1px solid #2a3a5c;margin-bottom:16px}
        .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:16px}
        .grid .card .value{font-size:22px;font-weight:700;margin-top:6px;color:#b0d4ff}
        .flex{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
        input,button{padding:10px 16px;border-radius:8px;border:1px solid #2a3a5c;background:#0f1729;color:#fff;font-size:14px}
        input{flex:1;min-width:200px}
        button{background:#1f3a5f;cursor:pointer;font-weight:600}
        button:hover{background:#2a4a7a}
        .btn-start{background:#0d7a3b}
        .btn-start:hover{background:#0f9d4a}
        .btn-stop{background:#7a2a2a}
        .btn-stop:hover{background:#a33a3a}
        .chart-wrap{background:#141b2b;border-radius:12px;padding:20px;border:1px solid #2a3a5c;margin-top:16px}
        .chart-wrap canvas{width:100% !important;height:280px !important}
        .footer{text-align:center;color:#4a5a7a;font-size:12px;border-top:1px solid #1f2a3f;padding-top:18px;margin-top:20px}
        .status-dot{display:inline-block;width:12px;height:12px;border-radius:50%;margin-left:5px}
        .dot-green{background:#4caf50}
        .dot-red{background:#f44336}
        .error-msg{background:#7a2a2a;border:1px solid #a33a3a;border-radius:8px;padding:8px 12px;margin-top:8px;color:#ff6b6b;font-size:13px;display:none}
        .badge{font-size:11px;color:#8899bb}
    </style>
</head>
<body>
<div class="container">
    <h1>⛏️ داشبورد ماینینگ</h1>
    <div class="card">
        <div class="badge">🔑 کیف پول</div>
        <div class="flex" style="margin-top:6px">
            <input type="text" id="walletInput" placeholder="آدرس کیف پول مونرو" value="48edfHu7V9Z84YzzMa6fUueoELZ9ZRXq9VetWzYGzKt52XU5xvqgzYnDK9URnRoJMk1j8nLwEVsaSWJ4fhdUyZijBGUicoD">
            <button class="btn-start" onclick="startMining()">▶️ شروع</button>
            <button class="btn-stop" onclick="stopMining()">⏹️ توقف</button>
        </div>
        <div id="statusMsg" style="margin-top:8px;font-size:13px;color:#80cbc4"></div>
        <div id="errorMsg" class="error-msg"></div>
    </div>
    <div class="grid">
        <div class="card"><div class="badge">هش‌ریت</div><div class="value" id="hr">--</div><div class="badge">وضعیت: <span id="statusText">غیرفعال</span></div></div>
        <div class="card"><div class="badge">شارهای پذیرفته</div><div class="value" id="shares">--</div><div class="badge">کیف پول: <span id="walletDisplay">--</span></div></div>
        <div class="card"><div class="badge">آپتایم</div><div class="value" id="uptime">--</div><div class="badge">آخرین: <span id="lastUpdate">--</span></div></div>
        <div class="card"><div class="badge">استخر</div><div class="value" id="poolStatus">--</div><div class="badge" id="poolName">--</div></div>
    </div>
    <div class="chart-wrap"><canvas id="chart"></canvas></div>
    <div class="footer">⚡ بهینه برای Railway · رم مصرفی < ۱GB</div>
</div>
<script>
let chartInstance=null, historyData=[];
async function startMining(){
    const wallet=document.getElementById('walletInput').value.trim();
    if(!wallet||wallet.length<5){alert('آدرس کیف پول را وارد کنید');return}
    document.getElementById('statusMsg').innerHTML='🔄 در حال راه‌اندازی...';
    document.getElementById('errorMsg').style.display='none';
    try{
        const res=await fetch('/api/start-mining',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({wallet})});
        const data=await res.json();
        if(res.ok) document.getElementById('statusMsg').innerHTML='✅ '+data.message;
        else {document.getElementById('statusMsg').innerHTML='❌ خطا: '+data.detail;document.getElementById('errorMsg').textContent='❌ '+data.detail;document.getElementById('errorMsg').style.display='block';}
    }catch(e){document.getElementById('statusMsg').innerHTML='❌ خطا در ارتباط'}
    fetchStatus();
}
async function stopMining(){
    document.getElementById('statusMsg').innerHTML='⏹️ در حال توقف...';
    try{
        const res=await fetch('/api/stop-mining',{method:'POST'});
        const data=await res.json();
        document.getElementById('statusMsg').innerHTML='✅ '+data.message;
    }catch(e){document.getElementById('statusMsg').innerHTML='❌ خطا'}
    fetchStatus();
}
async function fetchStatus(){
    try{
        const res=await fetch('/api/miner-status');
        const data=await res.json();
        document.getElementById('hr').textContent=data.running?(data.hashrate/1e3).toFixed(1)+' H/s':'--';
        document.getElementById('shares').textContent=data.shares||'--';
        document.getElementById('uptime').textContent=data.running?formatUptime(data.uptime):'--';
        document.getElementById('walletDisplay').textContent=data.wallet?data.wallet.slice(0,10)+'...':'--';
        document.getElementById('lastUpdate').textContent=data.last_update?new Date(data.last_update*1000).toLocaleTimeString('fa-IR'):'--';
        document.getElementById('statusText').innerHTML=data.running?'<span class="status-dot dot-green"></span> فعال':'<span class="status-dot dot-red"></span> غیرفعال';
        if(data.error){document.getElementById('errorMsg').textContent='⚠️ '+data.error;document.getElementById('errorMsg').style.display='block';}
        else document.getElementById('errorMsg').style.display='none';
        document.getElementById('poolStatus').textContent=data.running?'متصل':'قطع';
        document.getElementById('poolName').textContent=data.running?'pool.supportxmr.com':'--';
        if(data.running&&data.hashrate>0){historyData.push({time:new Date(),hashrate:data.hashrate});if(historyData.length>100)historyData.shift();updateChart();}
    }catch(e){console.error(e)}
}
function formatUptime(sec){const h=Math.floor(sec/3600),m=Math.floor((sec%3600)/60),s=sec%60;return h+'h '+m+'m '+s+'s'}
function updateChart(){
    const labels=historyData.map(p=>p.time.toLocaleTimeString('fa-IR'));
    const values=historyData.map(p=>p.hashrate/1e3);
    if(chartInstance){chartInstance.data.labels=labels;chartInstance.data.datasets[0].data=values;chartInstance.update();}
    else{
        const ctx=document.getElementById('chart').getContext('2d');
        chartInstance=new Chart(ctx,{type:'line',data:{labels:labels,datasets:[{label:'هش‌ریت (H/s)',data:values,borderColor:'#4fc3f7',backgroundColor:'rgba(79,195,247,0.1)',fill:true,tension:0.3,pointRadius:3,pointBackgroundColor:'#4fc3f7'}]},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{labels:{color:'#b0d4ff'}}},scales:{x:{ticks:{color:'#6a7fa0',maxTicksLimit:12}},y:{ticks:{color:'#6a7fa0'}}}}});
    }
}
fetchStatus();
setInterval(fetchStatus,10000);
</script>
</body></html>"""

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return HTMLResponse(HTML_PAGE)

@app.get("/health")
async def health():
    return {"status": "ok", "miner": miner_status["running"]}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
