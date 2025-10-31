FROM python:3.14.0-slim

WORKDIR /electrumx

# Install all dependencies (build + runtime) for leveldb
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential libleveldb-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy the entire electrumx source tree
COPY . /electrumx

# Install from local source with rapidjson support
RUN pip install --upgrade pip \
    && pip install /electrumx[rapidjson]

# Data directory for electrumx
RUN mkdir -p /var/lib/electrumx

# Environment defaults
ENV SERVICES="tcp://:50001"
ENV COIN=Bitcoin
ENV NET=mainnet
ENV DB_DIRECTORY=/var/lib/electrumx
ENV DAEMON_URL="http://username:password@hostname:port/"
ENV ALLOW_ROOT=true
ENV DB_ENGINE=leveldb
ENV MAX_SEND=10000000
ENV BANDWIDTH_UNIT_COST=50000
ENV CACHE_MB=2000

CMD ["electrumx_server"]


# To build electrumx run the following command:
#   `docker build -f Dockerfile -t electrumx ..`


# To run electrumx run the following command:
#   `docker run -d --net=host -v /home/electrumx/db/:/var/lib/electrumx -e DAEMON_URL="http://youruser:yourpass@localhost:8332" -e REPORT_SERVICES=tcp://example.com:50001 electrumx`


# To clean shutdown, send TERM signal to the running container eg.: 
#   `docker kill --signal="TERM" CONTAINER_ID`
