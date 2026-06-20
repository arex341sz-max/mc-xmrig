FROM miningcontainers/xmrig:latest

COPY config.json /xmrig/config.json

EXPOSE 8080

ENTRYPOINT ["./xmrig"]
