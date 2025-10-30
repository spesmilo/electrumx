FROM python:3.10.14-bullseye

WORKDIR /electrumx

# Install all dependencies (build + runtime) for rocksdb
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        libleveldb-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy the entire electrumx source tree
COPY . /electrumx

# Install from local source with rapidjson support
RUN pip install --upgrade pip \
    && pip install "Cython<3.0" \
    && pip install /electrumx[rapidjson]

# Data directory for electrumx
RUN mkdir -p /data

# Environment defaults
ENV SERVICES="tcp://:50001"
ENV COIN=Bitcoin
ENV NET=mainnet
ENV DB_DIRECTORY=/data
ENV DAEMON_URL="http://username:password@hostname:port/"
ENV ALLOW_ROOT=true
ENV DB_ENGINE=leveldb
ENV MAX_SEND=10000000
ENV BANDWIDTH_UNIT_COST=50000
ENV CACHE_MB=2000

CMD ["electrumx_server"]