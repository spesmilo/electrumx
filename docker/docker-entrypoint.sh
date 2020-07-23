#!/bin/bash
set -e

if [[ "$1" = "electrumx_server" ]]; then
    exec gosu electrumx "$@"
elif [[ "$1" == "electrumx_rpc" ]]; then
    exec gosu electrumx_rpc "$@"
else
    exec "$@"
fi
