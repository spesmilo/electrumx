#!/bin/bash

script_dir=$(dirname $(realpath $0))
rpc_user=quizzical_neumann
rpc_pass=133b57a60a84
node_host=127.0.0.1
node_port=18554
error_invalid_index='Traceback (most recent call last):
  File "<string>", line 1, in <module>
IndexError: list index out of range'
amount=0.001
fee=0.00005

pexec() {
    python -c "print($@)"
}

jv() {
    python -c "import sys,json;print(json.dumps(json.loads(sys.stdin.read())[$1]))"
}

_drpc() {
    local m=$1
    shift;
    local IFS=","
    local p=$*
    curl -s -K <(echo "user: $rpc_user:$rpc_pass") --data-binary "{\"jsonrpc\":\"1.0\",\"id\":\"curltext\",\"method\":\"$m\",\"params\":[$p]}" -H 'content-type:text/plain;' http://$node_host:$node_port
}

drpc() {
    _drpc $@ | jv "'result'"
}

erpc() {
    $script_dir/../electrumx_rpc $@
}

assert_equal() {
    if [ "$1" != "$2" ]
    then
        echo "$3: $1 != $2" 
        exit $PPID
    fi
}
assert_not_equal() {
    if [ "$1" == "$2" ]
    then
        echo "$3: $1 == $2" 
        exit $PPID
    fi
}

sync_blocks() {
    sleep 3
    while [ $(drpc getblockchaininfo | jv "'blocks'") -ne  $(erpc getinfo | jv "'db height'") ]
    do
        sleep 3
    done
}

wait_trx() {
    sleep 3
    while [ $(drpc gettransaction "$1" | jv "'confirmations'") -lt 2 ]
    do
        sleep 3
    done
    drpc getrawtransaction "$1" "true"
}

DB_DIRECTORY=$(mktemp -d) DAEMON_URL=http://$rpc_user:$rpc_pass@$node_host:$node_port SERVICES=rpc://:8000 COIN=DeFiChain NET=testnet LOG_LEVEL=debug $script_dir/../electrumx_server &
sync_blocks

a1=$(drpc getnewaddress '"a"' '"legacy"')
a2=$(drpc getnewaddress '"a"' '"legacy"')
p1=$(drpc getaddressinfo $a1 | jv "'pubkey'")
p2=$(drpc getaddressinfo $a2 | jv "'pubkey'")
ms=$(drpc createmultisig 2 "["$p1"",""$p2"]")
redeem=$(echo $ms | jv "'redeemScript'")
address=$(echo $ms | jv "'address'" | tr -d '"')


trx=$(drpc sendtoaddress "\"$address\"" $amount)
txo=$(wait_trx $trx | jv "'vout'")
out=$(echo $txo | jv 0)
err=$(echo $txo | jv 2 2>&1)
assert_equal "$err" "$error_invalid_index" "vout must be lenght 2: $txo"
if [ "$(echo $out | jv "'value'")" != "$amount" ]
then
    out=$(echo $txo | jv 1)
fi

sync_blocks
assert_equal "$(echo $out | jv "'value'")" $amount "invalid OUT"
assert_equal "\"$(erpc query -l 2 $address | grep UTXO | cut -d ' ' -f 4)\"" "$trx" "invalid TXID"
assert_equal "$(erpc query -l 2 $address | grep Balance | cut -d ' ' -f 2)" "$(echo $out | jv "'value'")" "invalid AMOUNT"
echo "multisig charged"

amount=$(pexec $amount-$fee)
txn=$(echo $out | jv "'n'")
spk=$(echo $out | jv "'scriptPubKey'" | jv "'hex'" )
address=$(drpc getnewaddress | tr -d '"')
raw=$(drpc createrawtransaction "[{\"txid\":"$trx",\"vout\":$txn}]" "[{\"$address\":$amount}]")
pk1=$(drpc dumpprivkey $a1)
pk2=$(drpc dumpprivkey $a2)
sig=$(drpc signrawtransactionwithkey $raw  "["$pk1"]" "[{\"txid\":"$trx",\"vout\":$txn,\"scriptPubKey\":"$spk",\"redeemScript\":"$redeem"}]" | jv "'hex'")
sig=$(drpc signrawtransactionwithkey $sig  "["$pk2"]" "[{\"txid\":"$trx",\"vout\":$txn,\"scriptPubKey\":"$spk",\"redeemScript\":"$redeem"}]" | jv "'hex'")
trx=$(drpc sendrawtransaction $sig)
txo=$(wait_trx $trx | jv "'vout'")
out=$(echo $txo | jv 0)
err=$(echo $txo | jv 1 2>&1)
assert_equal "$err" "$error_invalid_index" "vout must be lenght 1: $txo"

sync_blocks
assert_equal "$(echo $out | jv "'value'")" $amount "invalid OUT"
assert_equal "\"$(erpc query -l 2 $address | grep UTXO | cut -d ' ' -f 4)\"" "$trx" "invalid TXID"
assert_equal "$(erpc query -l 2 $address | grep Balance | cut -d ' ' -f 2)" "$(echo $out | jv "'value'")" "invalid AMOUNT"
echo "multisig spended"

erpc stop
echo "success"