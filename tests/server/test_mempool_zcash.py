"""Check Zcash mempool selection and fee propagation through the RPC/DB path."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from electrumx.lib.coins import Bitcoin, Coin, Komodo, Zcash, ZcashTestnet
from electrumx.lib.hash import hex_str_to_hash
from electrumx.server.mempool import MemPool, MemPoolAPI
from electrumx.server.mempool_zcash import ZcashMemPool, ZcashMemPoolTx


def test_coin_mempool_selection():
    assert Coin.MEMPOOL is MemPool
    assert Bitcoin.MEMPOOL is MemPool
    assert Komodo.MEMPOOL is MemPool
    assert Zcash.MEMPOOL is ZcashMemPool
    assert ZcashTestnet.MEMPOOL is ZcashMemPool


@pytest.mark.asyncio
@pytest.mark.parametrize('name,expected_fee,input_values', [
    ('381b65', 10000, []),              # v5 fully shielded
    ('f15bfc', 20000, []),              # v6 fully shielded
    ('57d7d9', 15000, []),              # v6 unshielding
    ('b5e074', 10000, [501849159, 0]),  # v5 shielding, synthetic prevout values
    ('5d9654', 10000, [780767240]),     # transparent, synthetic prevout value
])
async def test_zcash_fetch_accept_and_report_fee(name, expected_fee, input_values):
    fixture = json.loads((Path(__file__).parents[1] / 'transactions' /
                          f'zcash_mainnet_{name}.json').read_text())
    txid = hex_str_to_hash(fixture['txid'])
    raw = bytes.fromhex(fixture['hex'])
    missing_txid = b'M' * 32
    input_hashx = b'I' * 11
    expected_prevouts = tuple((hex_str_to_hash(i['txid']), i['vout'])
                             for i in fixture['inputs'])

    async def raw_transactions(txids):
        return [raw if value == fixture['txid'] else None for value in txids]

    async def lookup_utxos(prevouts):
        assert prevouts == expected_prevouts
        return [(input_hashx, value) for value in input_values]

    api = Mock(spec=MemPoolAPI)
    api.raw_transactions = AsyncMock(side_effect=raw_transactions)
    api.lookup_utxos = AsyncMock(side_effect=lookup_utxos)
    pool = Zcash.MEMPOOL(Zcash, api)
    tx_map, utxo_map = await pool._fetch_raw_txs_and_utxos(
        new_txids_rev={txid, missing_txid}, all_txids_rev={txid, missing_txid})
    assert set(tx_map) == {txid}  # A transaction evicted by the daemon is skipped.
    assert isinstance(tx_map[txid], ZcashMemPoolTx)
    assert tx_map[txid].fee_adjustment == fixture['fee_adjustment_zat']
    touched_hashxs, touched_outpoints = set(), set()
    deferred, unspent = pool._accept_transactions(
        tx_map=tx_map, utxo_map=utxo_map, touched_hashxs=touched_hashxs,
        touched_outpoints=touched_outpoints, topologically_sort=True)
    assert deferred == {}
    assert unspent == {}
    assert pool.txs[txid].fee == expected_fee
    for hashx in touched_hashxs:
        summaries = await pool.transaction_summaries(hashx)
        assert [(s.txid_rev, s.fee) for s in summaries] == [(txid, expected_fee)]
    pool._update_histogram(bin_size=1)
    rate = (10 * expected_fee // len(raw)) / 10
    assert pool.cached_compact_histogram == [(rate, len(raw))]


@pytest.mark.parametrize('pool_cls,parent_fee', [(MemPool, 70), (ZcashMemPool, 10)])
@pytest.mark.parametrize('topologically_sort', [False, True])
def test_fee_isolation_and_deferred_parent(pool_cls, parent_fee, topologically_sort):
    pool = pool_cls(Zcash, Mock(spec=MemPoolAPI))
    parent_id, child_id = b'P' * 32, b'C' * 32
    confirmed = (b'B' * 32, 0)
    hashx = b'X' * 11
    parent = ZcashMemPoolTx(
        prevouts=(confirmed,), in_pairs=None, out_pairs=((hashx, 30),),
        fee=0, size=100, fee_adjustment=-60)
    child = ZcashMemPoolTx(
        prevouts=((parent_id, 0),), in_pairs=None, out_pairs=((hashx, 20),),
        fee=0, size=100)
    pending = {child_id: child, parent_id: parent}
    touched_hashxs, touched_outpoints = set(), set()
    # Neither transaction can be accepted before its funding output is known.
    pending, _ = pool._accept_transactions(
        tx_map=pending, utxo_map={}, touched_hashxs=touched_hashxs,
        touched_outpoints=touched_outpoints, topologically_sort=topologically_sort)
    assert len(pending) == 2
    assert not pool.txs
    pending, unspent = pool._accept_transactions(
        tx_map=pending, utxo_map={confirmed: (hashx, 100)},
        touched_hashxs=touched_hashxs, touched_outpoints=touched_outpoints,
        topologically_sort=topologically_sort)
    if pending:
        pending, unspent = pool._accept_transactions(
            tx_map=pending, utxo_map=unspent, touched_hashxs=touched_hashxs,
            touched_outpoints=touched_outpoints, topologically_sort=topologically_sort)
    assert not pending and not unspent
    assert pool.txs[parent_id].fee == parent_fee
    assert pool.txs[child_id].fee == 10
    assert pool.txo_to_spender == {confirmed: parent_id, (parent_id, 0): child_id}
    assert pool.hashXs[hashx] == {parent_id, child_id}


def test_zcash_fee_adjustment_is_included_in_mempool_fee():
    pool = ZcashMemPool(Zcash, Mock(spec=MemPoolAPI))
    prevout = (b'P' * 32, 0)
    txid = b'T' * 32
    tx = ZcashMemPoolTx(
        prevouts=(prevout,), in_pairs=None, out_pairs=((b'X' * 11, 60),),
        fee=0, size=100, fee_adjustment=7,
    )
    deferred, _ = pool._accept_transactions(
        tx_map={txid: tx}, utxo_map={prevout: (b'X' * 11, 100)},
        touched_hashxs=set(), touched_outpoints=set(), topologically_sort=False,
    )

    assert deferred == {}
    assert pool.txs[txid].fee == 47


def test_zcash_negative_shielded_aware_fee_is_not_silently_clamped():
    pool = ZcashMemPool(Zcash, Mock(spec=MemPoolAPI))
    prevout = (b'P' * 32, 0)
    tx = ZcashMemPoolTx(
        prevouts=(prevout,), in_pairs=None, out_pairs=((b'X' * 11, 100),),
        fee=0, size=100, fee_adjustment=-1,
    )

    with pytest.raises(ValueError, match='negative shielded-aware'):
        pool._accept_transactions(
            tx_map={b'T' * 32: tx}, utxo_map={prevout: (b'X' * 11, 100)},
            touched_hashxs=set(), touched_outpoints=set(), topologically_sort=False,
        )
