# Copyright (c) 2016-2018, Neil Booth
# Copyright (c) 2017, the ElectrumX authors
#
# All rights reserved.
#
# See the file "LICENCE" for information about the copyright
# and warranty status of this software.

'''History by script hash (address).'''

import ast
import time
from collections import defaultdict
from typing import TYPE_CHECKING, Type, Optional, Dict, Sequence, Tuple, List
import itertools
from functools import partial

from aiorpcx import run_in_thread

import electrumx.lib.util as util
from electrumx.lib.hash import HASHX_LEN, hash_to_hex_str
from electrumx.lib.util import (pack_le_uint64, unpack_le_uint64,
                                pack_le_uint32, unpack_le_uint32,
                                pack_be_uint64, unpack_be_uint64)

if TYPE_CHECKING:
    from electrumx.server.storage import Storage


TXNUM_LEN = 5
TXNUM_PADDING = bytes(8 - TXNUM_LEN)
TXOUTIDX_LEN = 3
TXOUTIDX_PADDING = bytes(4 - TXOUTIDX_LEN)


def unpack_txnum(tx_numb: bytes) -> int:
    return unpack_be_uint64(TXNUM_PADDING + tx_numb)[0]


def pack_txnum(tx_num: int) -> bytes:
    return pack_be_uint64(tx_num)[-TXNUM_LEN:]


class History:

    DB_VERSIONS = (3, )
    STORE_INTERMEDIATE_STATUSHASH_EVERY_N_TXS = 5000

    db: Optional['Storage']

    def __init__(self):
        self.logger = util.class_logger(__name__, self.__class__.__name__)
        self.hist_db_tx_count = 0
        self.hist_db_tx_count_next = 0  # after next flush, next value for self.hist_db_tx_count
        self.db_version = max(self.DB_VERSIONS)
        self.upgrade_cursor = -1

        self._unflushed_hashxs = defaultdict(bytearray)  # type: Dict[bytes, bytearray]
        self._unflushed_hashxs_count = 0
        self._unflushed_txhash_to_txnum_map = {}  # type: Dict[bytes, int]
        self._unflushed_txo_to_spender = {}  # type: Dict[bytes, int]  # (tx_num+txout_idx)->tx_num
        # hashX -> list of (tx_num, status):
        self._unflushed_hashx_to_statushash = {}  # type: Dict[bytes, List[Tuple[int, bytes]]]
        self._unflushed_statushash_count = 0

        # Key: b'H' + address_hashX + tx_num
        # Value: <null>
        # ---
        # Key: b't' + tx_hash
        # Value: tx_num
        # ---
        # Key: b's' + tx_num + txout_idx
        # Value: tx_num
        # "which tx spent this TXO?" -- note that UTXOs are not stored.
        # ---
        # Key: b'S' + address_hashX + tx_num
        # Value: status_hash
        # Status hash of hashX including only txs up to tx_num.
        # An append-only cache of partial statuses: only reorg-safe depths are stored.
        self.db = None

    def open_db(
            self,
            *,
            db_class: Type['Storage'],
            for_sync: bool,
            utxo_db_tx_count: int,
    ) -> None:
        self.db = db_class('hist', for_sync)
        self.read_state()
        self.clear_excess(utxo_db_tx_count)

    def close_db(self):
        if self.db:
            self.db.close()
            self.db = None

    def read_state(self):
        state = self.db.get(b'\0state')
        if state:
            state = ast.literal_eval(state.decode())
            if not isinstance(state, dict):
                raise RuntimeError('failed reading state from history DB')
            self.db_version = state.get('db_version', 0)
            self.upgrade_cursor = state.get('upgrade_cursor', -1)
            self.hist_db_tx_count = state.get('hist_db_tx_count', 0)
            self.hist_db_tx_count_next = self.hist_db_tx_count

        if self.db_version not in self.DB_VERSIONS:
            msg = (f'your history DB version is {self.db_version} but '
                   f'this software only handles DB versions {self.DB_VERSIONS}')
            self.logger.error(msg)
            raise RuntimeError(msg)
        if self.db_version != max(self.DB_VERSIONS):
            pass  # call future upgrade logic here
        self.logger.info(f'history DB version: {self.db_version}')

    def clear_excess(self, utxo_db_tx_count: int) -> None:
        # self.hist_db_tx_count != utxo_db_tx_count might happen as
        # both DBs cannot be updated atomically
        # FIXME when advancing blocks, hist_db is flushed first, so its count can be higher;
        #       but when backing up (e.g. reorg), hist_db is flushed first as well,
        #       so its count can be lower?!
        #       Shouldn't we flush utxo_db first when backing up?
        if self.hist_db_tx_count <= utxo_db_tx_count:
            assert self.hist_db_tx_count == utxo_db_tx_count
            return

        self.logger.info('DB shut down uncleanly.  Scanning for '
                         'excess history flushes...')

        hkeys = []
        for db_key, db_val in self.db.iterator(prefix=b'H'):
            tx_numb = db_key[-TXNUM_LEN:]
            tx_num = unpack_txnum(tx_numb)
            if tx_num >= utxo_db_tx_count:
                hkeys.append(db_key)

        tkeys = []
        for db_key, db_val in self.db.iterator(prefix=b't'):
            tx_numb = db_val
            tx_num = unpack_txnum(tx_numb)
            if tx_num >= utxo_db_tx_count:
                tkeys.append(db_key)

        skeys = []
        for db_key, db_val in self.db.iterator(prefix=b's'):
            tx_numb1 = db_key[1:1+TXNUM_LEN]
            tx_numb2 = db_val
            tx_num1 = unpack_txnum(tx_numb1)
            tx_num2 = unpack_txnum(tx_numb2)
            if max(tx_num1, tx_num2) >= utxo_db_tx_count:
                skeys.append(db_key)

        self.logger.info(f'deleting {len(hkeys):,d} addr entries,'
                         f' {len(tkeys):,d} txs, and {len(skeys):,d} spends')

        self.hist_db_tx_count = utxo_db_tx_count
        self.hist_db_tx_count_next = self.hist_db_tx_count
        with self.db.write_batch() as batch:
            for key in itertools.chain(hkeys, tkeys, skeys):
                batch.delete(key)
            self.write_state(batch)

        self.logger.info('deleted excess history entries')

    def write_state(self, batch):
        '''Write state to the history DB.'''
        state = {
            'hist_db_tx_count': self.hist_db_tx_count,
            'db_version': self.db_version,
            'upgrade_cursor': self.upgrade_cursor,
        }
        batch.put(b'\0state', repr(state).encode())

    def add_unflushed(
            self,
            *,
            hashXs_by_tx: Sequence[Sequence[bytes]],
            first_tx_num: int,
            txhash_to_txnum_map: Dict[bytes, int],
            txo_to_spender_map: Dict[Tuple[bytes, int], bytes],  # (tx_hash, txout_idx) -> tx_hash
    ):
        unflushed_hashxs = self._unflushed_hashxs
        count = 0
        tx_num = None
        for tx_num, hashXs in enumerate(hashXs_by_tx, start=first_tx_num):
            tx_numb = pack_txnum(tx_num)
            hashXs = set(hashXs)
            for hashX in hashXs:
                unflushed_hashxs[hashX] += tx_numb
            count += len(hashXs)
        self._unflushed_hashxs_count += count
        if tx_num is not None:
            assert self.hist_db_tx_count_next + len(hashXs_by_tx) == tx_num + 1
            self.hist_db_tx_count_next = tx_num + 1

        self._unflushed_txhash_to_txnum_map.update(txhash_to_txnum_map)

        unflushed_spenders = self._unflushed_txo_to_spender
        get_txnum_for_txhash = self.get_txnum_for_txhash
        for (prev_hash, prev_idx), spender_hash in txo_to_spender_map.items():
            prev_txnum = get_txnum_for_txhash(prev_hash)
            assert prev_txnum is not None
            spender_txnum = get_txnum_for_txhash(spender_hash)
            assert spender_txnum is not None
            prev_idx_packed = pack_le_uint32(prev_idx)[:TXOUTIDX_LEN]
            prev_txnumb = pack_txnum(prev_txnum)
            unflushed_spenders[prev_txnumb+prev_idx_packed] = spender_txnum

    def unflushed_memsize(self):
        # note: the magic numbers here were estimated using util.deep_getsizeof
        hashXs = len(self._unflushed_hashxs) * 180 + self._unflushed_hashxs_count * TXNUM_LEN
        txs = 232 + 93 * len(self._unflushed_txhash_to_txnum_map)
        spenders = 102 + 113 * len(self._unflushed_txo_to_spender)
        statushashes = (232 + 100 * len(self._unflushed_hashx_to_statushash)
                        + 161 * self._unflushed_statushash_count)
        return hashXs + txs + spenders + statushashes

    def assert_flushed(self):
        assert not self._unflushed_hashxs
        assert not self._unflushed_txhash_to_txnum_map
        assert not self._unflushed_txo_to_spender

    def flush(self):
        start_time = time.monotonic()
        unflushed_hashxs = self._unflushed_hashxs
        chunks = util.chunks

        with self.db.write_batch() as batch:
            for hashX in sorted(unflushed_hashxs):
                for tx_num in sorted(chunks(unflushed_hashxs[hashX], TXNUM_LEN)):
                    db_key = b'H' + hashX + tx_num
                    batch.put(db_key, b'')
            for tx_hash, tx_num in sorted(self._unflushed_txhash_to_txnum_map.items()):
                db_key = b't' + tx_hash
                tx_numb = pack_txnum(tx_num)
                batch.put(db_key, tx_numb)
            for prevout, spender_txnum in sorted(self._unflushed_txo_to_spender.items()):
                db_key = b's' + prevout
                db_val = pack_txnum(spender_txnum)
                batch.put(db_key, db_val)
            for hashX, lst in sorted(self._unflushed_hashx_to_statushash.items()):
                for tx_num, status in lst:
                    db_key = b'S' + hashX + pack_txnum(tx_num)
                    batch.put(db_key, status)
            self.hist_db_tx_count = self.hist_db_tx_count_next
            self.write_state(batch)

        addr_count = len(unflushed_hashxs)
        tx_count = len(self._unflushed_txhash_to_txnum_map)
        spend_count = len(self._unflushed_txo_to_spender)
        statushash_count = self._unflushed_statushash_count
        unflushed_hashxs.clear()
        self._unflushed_hashxs_count = 0
        self._unflushed_txhash_to_txnum_map.clear()
        self._unflushed_txo_to_spender.clear()
        self._unflushed_statushash_count = 0
        self._unflushed_hashx_to_statushash.clear()

        if self.db.for_sync:
            elapsed = time.monotonic() - start_time
            self.logger.info(f'flushed history in {elapsed:.1f}s, for: '
                             f'{addr_count:,d} addrs, {tx_count:,d} txs, {spend_count:,d} spends, '
                             f'{statushash_count:,d} statushashes')

    def backup(self, *, hashXs, tx_count, tx_hashes: Sequence[bytes], spends: Sequence[bytes]):
        self.assert_flushed()
        get_txnum_for_txhash = self.get_txnum_for_txhash
        nremoves_addr = 0
        with self.db.write_batch() as batch:
            for hashX in sorted(hashXs):
                deletes = []
                prefix = b'H' + hashX
                for db_key, db_val in self.db.iterator(prefix=prefix, reverse=True):
                    tx_numb = db_key[-TXNUM_LEN:]
                    tx_num = unpack_txnum(tx_numb)
                    if tx_num >= tx_count:
                        nremoves_addr += 1
                        deletes.append(db_key)
                    else:
                        # note: we can break now, due to 'reverse=True' and txnums being big endian
                        break
                for key in deletes:
                    batch.delete(key)
            for spend in spends:
                prev_hash = spend[:32]
                prev_idx = spend[32:]
                assert len(prev_idx) == TXOUTIDX_LEN
                prev_txnum = get_txnum_for_txhash(prev_hash)
                assert prev_txnum is not None
                prev_txnumb = pack_txnum(prev_txnum)
                db_key = b's' + prev_txnumb + prev_idx
                batch.delete(db_key)
            for tx_hash in sorted(tx_hashes):
                db_key = b't' + tx_hash
                batch.delete(db_key)
            self.hist_db_tx_count = tx_count
            self.hist_db_tx_count_next = self.hist_db_tx_count
            self.write_state(batch)

        self.logger.info(f'backing up history, removed {nremoves_addr:,d} addrs, '
                         f'{len(tx_hashes):,d} txs, and {len(spends):,d} spends')

    def get_txnums(
            self,
            *,
            hashX: bytes,
            limit: Optional[int] = 1000,
            txnum_min: Optional[int] = None,
            txnum_max: Optional[int] = None,
    ):
        '''Generator that returns an unpruned, sorted list of tx_nums in the
        history of a hashX.  Includes both spending and receiving
        transactions.  By default yields at most 1000 entries.  Set
        limit to None to get them all.
        txnum_min can be used to seek into the history and start there (>=) (instead of genesis).
        txnum_max can be used to stop early (<).
        '''
        limit = util.resolve_limit(limit)
        prefix = b'H' + hashX
        it = self.db.iterator(prefix=prefix)
        if txnum_min is not None:
            it.seek(prefix + pack_txnum(txnum_min))
        txnum_min = txnum_min if txnum_min is not None else 0
        txnum_max = txnum_max if txnum_max is not None else float('inf')
        assert txnum_min <= txnum_max, f"txnum_min={txnum_min}, txnum_max={txnum_max}"
        for db_key, db_val in it:
            tx_numb = db_key[-TXNUM_LEN:]
            if limit == 0:
                return
            tx_num = unpack_txnum(tx_numb)
            if tx_num >= txnum_max:
                return
            assert txnum_min <= tx_num < txnum_max, (f"txnum_min={txnum_min}, tx_num={tx_num}, "
                                                     f"txnum_max={txnum_max}")
            yield tx_num
            limit -= 1

    def get_txnum_for_txhash(self, tx_hash: bytes) -> Optional[int]:
        tx_num = self._unflushed_txhash_to_txnum_map.get(tx_hash)
        if tx_num is None:
            db_key = b't' + tx_hash
            tx_numb = self.db.get(db_key)
            if tx_numb:
                tx_num = unpack_txnum(tx_numb)
        return tx_num

    def get_spender_txnum_for_txo(self, prev_txnum: int, txout_idx: int) -> Optional[int]:
        '''For an outpoint, returns the tx_num that spent it.
        If the outpoint is unspent, or even if it never existed (!), returns None.
        '''
        prev_idx_packed = pack_le_uint32(txout_idx)[:TXOUTIDX_LEN]
        prev_txnumb = pack_txnum(prev_txnum)
        prevout = prev_txnumb + prev_idx_packed
        spender_txnum = self._unflushed_txhash_to_txnum_map.get(prevout)
        if spender_txnum is None:
            db_key = b's' + prevout
            spender_txnumb = self.db.get(db_key)
            if spender_txnumb:
                spender_txnum = unpack_txnum(spender_txnumb)
        return spender_txnum

    def fs_get_intermediate_statushash_for_hashx(
            self,
            *,
            hashX: bytes,
            txnum_max: int = None,
    ) -> Tuple[int, bytes]:
        '''For a hashX, returns (tx_num, status), with the latest stored statushash
        and corresponding tx_num, where tx_num < txnum_max.
        This can be used to efficiently calculate the status of a hashX as
        only the txs mined after(>) tx_num will need to be hashed.
        '''
        # first, search in-memory, among the unflushed statuses
        unflushed_statushashes = self._unflushed_hashx_to_statushash.get(hashX, [])
        if len(unflushed_statushashes) > 0:
            for tx_num, status in reversed(unflushed_statushashes):
                if txnum_max is None or tx_num < txnum_max:
                    return tx_num, status
        # second, search in the on-disk DB
        prefix = b'S' + hashX
        it = self.db.iterator(prefix=prefix, reverse=True)
        if txnum_max is not None:
            it.seek(prefix + pack_txnum(txnum_max))
        for db_key, db_val in it:
            tx_numb = db_key[-TXNUM_LEN:]
            tx_num = unpack_txnum(tx_numb)
            status = db_val
            break
        else:
            tx_num = 0
            status = bytes(32)
        return tx_num, status

    async def get_intermediate_statushash_for_hashx(
            self,
            *,
            hashX: bytes,
            txnum_max: int = None,
    ) -> Tuple[int, bytes]:
        f = partial(self.fs_get_intermediate_statushash_for_hashx, hashX=hashX, txnum_max=txnum_max)
        return await run_in_thread(f)

    def store_intermediate_statushash_for_hashx(
            self,
            *,
            hashX: bytes,
            tx_num: int,
            status: bytes,
    ) -> None:
        '''For a hashX, store a partial status calculated up to (and including) tx_num.
        tx_num must be at a reorg-safe depth.
        The status is only stored in memory at first; it will be written to the DB
        during the next flush().
        '''
        if hashX not in self._unflushed_hashx_to_statushash:
            self._unflushed_hashx_to_statushash[hashX] = []
        # maintain invariant that unflushed statuses are in order (increasing tx_num):
        if len(self._unflushed_hashx_to_statushash[hashX]) > 0:
            tx_num_last, status_last = self._unflushed_hashx_to_statushash[hashX][-1]
            if tx_num <= tx_num_last:
                return
        self._unflushed_hashx_to_statushash[hashX].append((tx_num, status))
        self._unflushed_statushash_count += 1
