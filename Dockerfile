FROM python:3.10-slim

RUN apt-get update && apt-get install -y \
    build-essential \
    libssl-dev \
    libevent-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . /electrumx
WORKDIR /electrumx

EXPOSE 50001 50002

CMD ["python3", "-m", "electrumx.server.controller"]
