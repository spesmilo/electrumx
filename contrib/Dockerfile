# example of Dockerfile that installs spesmilo electrumx 1.16.0
# ENV variables can be overriden on the `docker run` command

FROM python:3.9.16-bullseye AS builder

WORKDIR /usr/src/app

# Install the libs needed by rocksdb (including development headers)
RUN apt-get update \
    && apt-get -y --no-install-recommends install librocksdb-dev libsnappy-dev libbz2-dev libz-dev liblz4-dev \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv venv \
    && venv/bin/pip install --no-cache-dir Cython==0.29.21 \
    && venv/bin/pip install --no-cache-dir e-x==1.16.0 \
    && venv/bin/pip install --no-cache-dir python-rapidjson==1.11 \
    && venv/bin/pip install --no-cache-dir python-rocksdb==0.7.0

FROM python:3.9.16-slim-bullseye

# Install the libs needed by rocksdb (no development headers or statics)
RUN apt-get update \
    && apt-get -y --no-install-recommends install librocksdb6.11 libsnappy1v5 libbz2-1.0 zlib1g liblz4-1 \
    && rm -rf /var/lib/apt/lists/*

ENV HOME /data
ENV DB_DIRECTORY=/data
ENV SSL_CERTFILE ${DB_DIRECTORY}/electrumx.crt
ENV SSL_KEYFILE ${DB_DIRECTORY}/electrumx.key
ENV ALLOW_ROOT=1
ENV DB_ENGINE=rocksdb
ENV MAX_SEND=10000000
ENV BANDWIDTH_UNIT_COST=50000
ENV CACHE_MB=2000

WORKDIR /usr/src/app
COPY --from=builder /usr/src/app .

RUN mkdir -p "$DB_DIRECTORY" && ulimit -n 1048576

VOLUME ["/data"]

WORKDIR /data
RUN openssl req -newkey rsa:2048 -sha256 -nodes -x509 -days 365 -subj "/O=ElectrumX" -keyout electrumx.key -out electrumx.crt

CMD ["/usr/src/app/venv/bin/python", "/usr/src/app/venv/bin/electrumx_server"]

# build it with eg.: `docker build -t electrumx .`
# run it with eg.:
# `docker run -d --net=host -v /home/electrumx/db/:/var/lib/electrumx -e DAEMON_URL="http://youruser:yourpass@localhost:8332" -e REPORT_SERVICES=tcp://example.com:50001 electrumx`
# for a clean shutdown, send TERM signal to the running container eg.: `docker kill --signal="TERM" CONTAINER_ID`
