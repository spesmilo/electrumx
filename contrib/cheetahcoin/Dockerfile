FROM ubuntu:22.04

WORKDIR /

RUN apt-get update && \
        apt-get -y install python3-setuptools python3-multidict python3.10 python3.10-dev libleveldb-dev python3-setuptools python3-multidict gcc g++ libsnappy-dev zlib1g-dev libbz2-dev libgflags-dev build-essential python3-pip git

RUN python3.10 -m pip install --upgrade pip
RUN python3.10 -m pip install aiohttp pylru Cython uvloop quark_hash
RUN python3.10 -m pip install plyvel
RUN git clone https://github.com/ShorelineCrypto/electrumx-1 /opt/electrumx

ENV COIN=Cheetahcoin
ENV DB_DIRECTORY=/db
ENV DAEMON_URL="http://RPCUSER:RPCPASSWORD@IP:RPCPORT/"
ENV SERVICES="tcp://:10007,rpc://0.0.0.0:8007,ssl://:10008,wss://:10009"
ENV EVENT_LOOP_POLICY=uvloop
ENV PEER_DISCOVERY=self
ENV INITIAL_CONCURRENT=50
ENV COST_SOFT_LIMIT=10000
ENV COST_HARD_LIMIT=100000
ENV BANDWIDTH_UNIT_COST=10000
ENV SSL_CERTFILE=/ssl/fullchain.pem
ENV SSL_KEYFILE=/ssl/privkey.pem
ENV ALLOW_ROOT=true
ENV MAX_SEND=10000000
ENV CACHE_MB=2000

VOLUME /db
VOLUME /ssl

RUN mkdir -p "$DB_DIRECTORY" && ulimit -n 1048576
RUN mkdir -p /ssl

WORKDIR  /opt/electrumx

RUN python3.10 setup.py install
CMD ["/usr/bin/python3.10", "/opt/electrumx/electrumx_server"]

