# Copyright (c) 2017, the ElectrumX authors
#
# All rights reserved.
#
# The MIT License (MIT)
#
# Permission is hereby granted, free of charge, to any person obtaining
# a copy of this software and associated documentation files (the
# "Software"), to deal in the Software without restriction, including
# without limitation the rights to use, copy, modify, merge, publish,
# distribute, sublicense, and/or sell copies of the Software, and to
# permit persons to whom the Software is furnished to do so, subject to
# the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE
# LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION
# WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
# and warranty status of this software.

import json
import os
from binascii import unhexlify

import pytest

from electrumx.lib import coins
from electrumx.lib.coins import Coin
from electrumx.lib.hash import hex_str_to_hash
from electrumx.lib.util import subclasses


def _does_coin_require_testcase(coin: Coin) -> bool:
    if coin.NET != 'mainnet':
        return False
    # legacy whitelist: these coins do not have tests. FIXME
    if coin in [
        coins.Bitcoin,
        coins.BitcoinCash,
        coins.Viacoin,
        coins.Argentum,
        coins.FairCoin,
        coins.Einsteinium,
        coins.Crown,
        coins.Monaize,
        coins.Bitbay,
        coins.Fujicoin,
        coins.Neblio,
        coins.Bitzeny,
        coins.Sibcoin,
        coins.CanadaeCoin,
        coins.Auroracoin,
    ]:
        return False
    return True


coin_classes_all = set([coin for coin in subclasses(Coin) if _does_coin_require_testcase(coin)])
coin_classes_tested = set()

BLOCKS_DIR = os.path.join(
    os.path.dirname(os.path.realpath(__file__)), 'blocks')

# Find out which db engines to test
# Those that are not installed will be skipped
blocks = []

for name in os.listdir(BLOCKS_DIR):
    try:
        name_parts = name.split("_")
        coin = Coin.lookup_coin_class(name_parts[0], name_parts[1])
        if _does_coin_require_testcase(coin):
            coin_classes_tested.add(coin)
        with open(os.path.join(BLOCKS_DIR, name)) as f:
            blocks.append((coin, json.load(f)))
    except Exception as e:
        blocks.append(pytest.fail(name))


@pytest.fixture(params=blocks)
def block_details(request):
    return request.param


def test_block(block_details):
    coin, block_info = block_details

    raw_block = unhexlify(block_info['block'])
    block = coin.block(raw_block, block_info['height'])

    try:
        assert coin.header_hash(
            block.header) == hex_str_to_hash(block_info['hash'])
    except ImportError as e:
        pytest.skip(str(e))
    assert (coin.header_prevhash(block.header)
            == hex_str_to_hash(block_info['previousblockhash']))
    assert len(block_info['tx']) == len(block.transactions)
    for n, tx in enumerate(block.transactions):
        assert tx.txid == hex_str_to_hash(block_info['tx'][n])


def test_all_coins_are_covered():
    assert coin_classes_all - coin_classes_tested == set()
