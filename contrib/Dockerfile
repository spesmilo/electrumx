# ElectrumX for Bitcoin with LevelDB
# Build: docker build -t electrumx .
#        podman build -t electrumx .
# Run:   docker run -d --net=host -v ~/electrumx-db:/var/lib/electrumx -e DAEMON_URL="http://youruser:yourpass@localhost:8332" -e REPORT_SERVICES=tcp://example.com:50001 electrumx
#        podman run -d --net=host -v ~/electrumx-db:/var/lib/electrumx -e DAEMON_URL="http://youruser:yourpass@localhost:8332" -e REPORT_SERVICES=tcp://example.com:50001 electrumx
# Stop:  docker kill --signal=TERM CONTAINER_ID
#        podman kill --signal=TERM CONTAINER_ID

FROM python:3.14.3-trixie AS builder

WORKDIR /usr/src/app

# Install the libs needed by leveldb (including development headers)
RUN apt-get update \
    && apt-get -y --no-install-recommends install \
        build-essential git libleveldb-dev libsnappy-dev \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv venv \
    && venv/bin/pip install --no-cache-dir \
        "e-x[rapidjson,leveldb] @ git+https://github.com/spesmilo/electrumx.git@1.19.0"


FROM python:3.14.3-slim-trixie

# Install the libs needed by leveldb (no development headers or statics)
RUN apt-get update \
    && apt-get -y --no-install-recommends install \
        libleveldb1d libsnappy1v5 \
    && rm -rf /var/lib/apt/lists/*

ENV SERVICES="tcp://:50001"
ENV COIN=Bitcoin
ENV DB_DIRECTORY=/var/lib/electrumx
ENV DAEMON_URL="http://username:password@hostname:port/"
ENV ALLOW_ROOT=true
ENV DB_ENGINE=leveldb
ENV MAX_SEND=10000000
ENV BANDWIDTH_UNIT_COST=50000
ENV CACHE_MB=2000

WORKDIR /usr/src/app
COPY --from=builder /usr/src/app .

VOLUME /var/lib/electrumx

RUN mkdir -p "$DB_DIRECTORY"

CMD ["/usr/src/app/venv/bin/python", "/usr/src/app/venv/bin/electrumx_server"]
