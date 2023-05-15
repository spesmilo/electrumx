#!/bin/bash
while ./electrumx_server; exitcode=$? && test $exitcode -eq 65;
do
    echo Attempting automatic electrumx_compact_history.
    ./electrumx_compact_history
    # exit on compaction failure
    if test $? -ne 0
    then
        exit 2
    fi
done
exit $exitcode
