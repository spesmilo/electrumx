#!/bin/bash

mkdir -p ~/.electrumx
export DB_DIRECTORY=~/.electrumx
export COIN=Bitcoin
export DAEMON_URL=bitcoin_rpc_user:bitcoin_rpc_pass@localhost

python3.7 ./electrumx_server
