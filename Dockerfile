FROM miningcontainers/xmrig:latest

# کانفیگ را به مسیر درست داخل کانتینر کپی می‌کنیم
COPY config.json /xmrig/config.json

# دستور اجرا: خود xmrig فایل config.json را به‌صورت خودکار پیدا می‌کند
ENTRYPOINT ["./xmrig"]
