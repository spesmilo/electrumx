# ElectrumX BGL - python electrum server for Bitgesell

<img src="Icon.png" style="height: 60px;" />

```
Licence: MIT
Original Author: Neil Booth
Current Maintainers: Sevault Wallet Maintainers, murgornaftali[at]gmail.com
Language: Python (>= 3.10)
```


This project is a fork of [kyuupichan/electrumx](https://github.com/kyuupichan/electrumx) with an added support for Bitgesell

ElectrumX allows users to run their own Electrum server. It connects to your
full node and indexes the blockchain, allowing efficient querying of the history of
arbitrary addresses. The server can be exposed publicly, and joined to the public network
of servers via peer discovery. As of May 2020, a significant chunk of the public
Electrum server network runs ElectrumX.

### Documentation

See [readthedocs](https://electrumx-spesmilo.readthedocs.io).

### Deployment
This Electrumx server implementation currently powers this [Bitgesell Explorer](https://bgl.sevaultwallet.com)

Deployment with Docker:

* `electrumx.conf` (config file)
* `Dockerfile` (for containerized deployment)
* `docker run` command

---

## ✅ 1. `electrumx.conf`

Save this as `/etc/electrumx.conf` or bind-mount into Docker:

```ini
COIN = Bitgesell
NET = mainnet

# Bitgesell node RPC
DAEMON_URL = http://rpcuser:rpcpassword@host:port/

# ElectrumX DB directory
DB_DIRECTORY = /electrumx/db

# Networking
TCP_PORT = 50001
SSL_PORT = 50002
HOST = 0.0.0.0

# Performance and Limits
COST_SOFT_LIMIT = 0
COST_HARD_LIMIT = 0
MAX_SEND = 10000000
MAX_SESSIONS = 100
BANNER_FILE = /electrumx/banner

# Logging
LOG_LEVEL = info
```

## ✅ 2. `Dockerfile`

Place this in the root of the ElectrumX fork:

```Dockerfile
FROM python:3.10-slim

RUN apt-get update && apt-get install -y \
    build-essential \
    libssl-dev \
    libevent-dev \
    && rm -rf /var/lib/apt/lists/*

# Install ElectrumX dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy your ElectrumX code
COPY . /electrumx
WORKDIR /electrumx

EXPOSE 50001 50002

CMD ["python3", "-m", "electrumx.server.controller"]
```

## ✅ 3. Docker Build & Run

```bash
docker build -t electrumx-bgl .
```

Then run it:

```bash
docker run -d \
  --name electrumx-bgl \
  -p 50001:50001 \
  -p 50002:50002 \
  -v $HOME/electrumx/db:/electrumx/db \
  -v $HOME/electrumx/banner:/electrumx/banner \
  -v $HOME/electrumx/electrumx.conf:/etc/electrumx.conf \
  --restart always \
  electrumx-bgl
```

Replace `$HOME/electrumx/...` with your actual file paths.

---

## 🧪 Test Connection

From your host or client:

```bash
nc your-vps-ip 50001
```

Send:

```json
{"id":1,"method":"server.version","params":["2.9.0", "1.4"]}
```

Expected response:

```json
{"id":1,"result":["ElectrumX X.Y.Z", "1.4"]}
```


