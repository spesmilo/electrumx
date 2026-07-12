# ElectrumX for Bitcoin, with RocksDB
# This is a simple ~minimal example Dockerfile.
#
# Build: docker build --pull -t electrumx .
#        podman build --pull  -t electrumx .
# Run:   docker run -d --net=host -v ~/electrumx-db:/var/lib/electrumx -e DAEMON_URL="http://youruser:yourpass@localhost:8332" -e REPORT_SERVICES=tcp://example.com:50001 electrumx
#        podman run -d --net=host -v ~/electrumx-db:/var/lib/electrumx -e DAEMON_URL="http://youruser:yourpass@localhost:8332" -e REPORT_SERVICES=tcp://example.com:50001 electrumx
# Stop:  docker stop CONTAINER_ID
#        podman stop CONTAINER_ID

FROM python:3.14-trixie AS builder

WORKDIR /usr/src/app

# Install the libs needed by rocksdb (including development headers)
RUN apt-get update \
    && apt-get -y --no-install-recommends install \
        build-essential \
        git \
        librocksdb-dev \
        pkg-config \
    && rm -rf /var/lib/apt/lists/*

# Install ElectrumX
ARG ELECTRUMX_VERSION=2.0.0
#ARG ELECTRUMX_VERSION=b8f6e227f251fe5cba96b940ffaa094439636417
RUN python -m venv venv \
    && venv/bin/pip install --no-cache-dir \
        "e_x[rocksdb] @ git+https://github.com/spesmilo/electrumx.git@${ELECTRUMX_VERSION}"


FROM python:3.14-slim-trixie

# Install the libs needed by rocksdb (no development headers or statics)
RUN apt-get update && apt-get -y --no-install-recommends install librocksdb9.10 && rm -rf /var/lib/apt/lists/*

ENV SERVICES="tcp://:50001"
ENV COIN=Bitcoin
ENV DB_DIRECTORY=/var/lib/electrumx
ENV DAEMON_URL="http://username:password@hostname:port/"
ENV ALLOW_ROOT=true
ENV DB_ENGINE=rocksdb
#ENV CACHE_MB=2000

WORKDIR /usr/src/app
COPY --from=builder /usr/src/app .

VOLUME /var/lib/electrumx

RUN mkdir -p "$DB_DIRECTORY"

CMD ["/usr/src/app/venv/bin/python", "/usr/src/app/venv/bin/electrumx_server"]
