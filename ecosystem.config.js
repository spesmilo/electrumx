module.exports = {
  apps: [{
    name: 'electrumx-doriancoin',
    script: './electrumx_server',
    cwd: '/home/electrumx/electrumx',
    interpreter: '/home/electrumx/electrumx/venv/bin/python3',
    env: {
      COIN: 'Doriancoin',
      NET: 'mainnet',
      DB_DIRECTORY: '/var/lib/electrumx/doriancoin',
      DAEMON_URL: 'http://user:password@127.0.0.1:1948/',
      SERVICES: 'tcp://:51001,ssl://:51002',
      SSL_CERTFILE: '/home/electrumx/.electrumx/certs/server.crt',
      SSL_KEYFILE: '/home/electrumx/.electrumx/certs/server.key',
      CACHE_MB: '2000',
      BANDWIDTH_UNIT_COST: '100000',
      REQUEST_TIMEOUT: '60',
      MAX_RECV: '50000000',
      MAX_SEND: '20000000',
      DB_BATCH_SIZE: '10000',
      COST_SOFT_LIMIT: '2000',
      COST_HARD_LIMIT: '100000',
      REQUEST_SLEEP: '5000',
    },
    autorestart: true,
    max_restarts: 10,
    restart_delay: 5000,
  }]
};
