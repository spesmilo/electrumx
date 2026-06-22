# Copyright (c) 2016-2018, Neil Booth
# Copyright (c) 2017, the ElectrumX authors
#
# All rights reserved.
#
# See the file "LICENCE" for information about the copyright
# and warranty status of this software.

'''History by script hash (address).'''

import ast
import bisect
import time
from array import array
from collections import defaultdict
from typing import TYPE_CHECKING, Type, Optional, Sequence, Iterable

import electrumx.lib.util as util
from electrumx.server.db_util import (
    DBTooOldForMigrations,
    pack_txnum, unpack_txnum, TXNUM_LEN,
)

if TYPE_CHECKING:
    from electrumx.server.storage import Storage


class History:

    DB_VERSIONS = (4, )
    DB_STATE_KEY = b'state\0\0'

    db: Optional['Storage']

    def __init__(self):
        self.logger = util.class_logger(__name__, self.__class__.__name__)
        self.unflushed = defaultdict(bytearray)
        self.unflushed_count = 0
        self.hist_db_tx_count = 0
        self.hist_db_tx_count_next = 0  # after next flush, next value for self.hist_db_tx_count
        self.db_version = max(self.DB_VERSIONS)
        self.upgrade_cursor = -1

        # Key: b'H' + address_hashX + tx_num
        # Value: <null>
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

    def close_db(self) -> None:
        if self.db:
            self.db.close()
            self.db = None

    def read_state(self) -> None:
        state = self.db.get(self.DB_STATE_KEY)
        if state:
            state = ast.literal_eval(state.decode())
            if not isinstance(state, dict):
                raise RuntimeError('failed reading state from history DB')
            self.db_version = state.get('db_version', 0)
            self.upgrade_cursor = state.get('upgrade_cursor', -1)
            self.hist_db_tx_count = state.get('hist_db_tx_count', 0)
            self.hist_db_tx_count_next = self.hist_db_tx_count

        if self.db_version not in self.DB_VERSIONS:
            raise DBTooOldForMigrations(
                db_name="history", db_version=self.db_version, supported_versions=self.DB_VERSIONS)
        if self.db_version != max(self.DB_VERSIONS):
            raise Exception("missing db upgrade")  # call future upgrade logic here
        self.logger.info(f'history DB version: {self.db_version}')

    def clear_excess(self, utxo_db_tx_count: int) -> None:
        # self.hist_db_tx_count != utxo_db_tx_count might happen as
        # both DBs cannot be updated atomically.
        # However, we MUST maintain the invariant that hist_db_tx_count >= utxo_db_tx_count:
        # - when advancing blocks, hist_db is flushed first, so its count can be higher;
        #   but when backing up (during reorg), hist_db is flushed last, so its count can be higher.
        if self.hist_db_tx_count == utxo_db_tx_count:
            return  # happy path
        elif self.hist_db_tx_count < utxo_db_tx_count:
            raise RuntimeError(f"corrupted DB. {self.hist_db_tx_count=} < {utxo_db_tx_count=}")

        assert self.hist_db_tx_count > utxo_db_tx_count
        self.logger.info(
            'DB shut down uncleanly. Scanning for excess history flushes... '
            'This might take a loooong time. :/'
        )

        utxo_db_tx_count_bytes = pack_txnum(utxo_db_tx_count)
        keys = []  # FIXME could grow large in-memory?
        for iter_cnt, (db_key, _db_val) in enumerate(self.db.iterator(prefix=b'H')):
            if iter_cnt % 10_000_000 == 0:
                self.logger.info(
                    f'Scanning for excess history flushes... num history entries checked: '
                    f'{iter_cnt // 1_000_000} million. will delete: {len(keys):,d}')
            tx_numb = db_key[-TXNUM_LEN:]
            # we exploit that pack_txnum uses big-endian byte-order, and just compare the bytes directly:
            # tx_num = unpack_txnum(tx_numb)
            if tx_numb >= utxo_db_tx_count_bytes:
                keys.append(db_key)

        self.logger.info(f'deleting {len(keys):,d} history entries')

        self.hist_db_tx_count = utxo_db_tx_count
        self.hist_db_tx_count_next = self.hist_db_tx_count
        with self.db.write_batch() as batch:
            for key in keys:
                batch.delete(key)
            self._write_state(batch)

        self.logger.info('deleted excess history entries')

    def _write_state(self, batch) -> None:
        '''Write state to the history DB.'''
        state = {
            'hist_db_tx_count': self.hist_db_tx_count,
            'db_version': self.db_version,
            'upgrade_cursor': self.upgrade_cursor,
        }
        batch.put(self.DB_STATE_KEY, repr(state).encode())

    def add_unflushed(self, hashXs_by_tx: list[list[bytes]], first_tx_num: int) -> None:
        unflushed = self.unflushed
        count = 0
        tx_num = None
        for tx_num, hashXs in enumerate(hashXs_by_tx, start=first_tx_num):
            tx_numb = pack_txnum(tx_num)
            hashXs = set(hashXs)
            for hashX in hashXs:
                unflushed[hashX] += tx_numb
            count += len(hashXs)
        self.unflushed_count += count
        if tx_num is not None:
            assert self.hist_db_tx_count_next + len(hashXs_by_tx) == tx_num + 1
            self.hist_db_tx_count_next = tx_num + 1

    def unflushed_memsize(self) -> int:
        return len(self.unflushed) * 180 + self.unflushed_count * TXNUM_LEN

    def assert_flushed(self) -> None:
        assert not self.unflushed

    def flush(self) -> None:
        start_time = time.monotonic()
        unflushed = self.unflushed
        chunks = util.chunks

        with self.db.write_batch() as batch:
            for hashX in sorted(unflushed):
                for tx_num in chunks(unflushed[hashX], TXNUM_LEN):
                    db_key = b'H' + hashX + tx_num
                    batch.put(db_key, b'')
            self.hist_db_tx_count = self.hist_db_tx_count_next
            self._write_state(batch)

        count = len(unflushed)
        unflushed.clear()
        self.unflushed_count = 0

        if self.db.for_sync:
            elapsed = time.monotonic() - start_time
            self.logger.info(f'flushed history in {elapsed:.1f}s '
                             f'for {count:,d} addrs')

    def backup(self, *, hashXs: Iterable[bytes], tx_count: int) -> None:
        self.assert_flushed()
        nremoves = 0
        with self.db.write_batch() as batch:
            for hashX in sorted(hashXs):
                deletes = []
                prefix = b'H' + hashX
                for db_key, db_val in self.db.iterator(prefix=prefix, reverse=True):
                    tx_numb = db_key[-TXNUM_LEN:]
                    tx_num = unpack_txnum(tx_numb)
                    if tx_num >= tx_count:
                        nremoves += 1
                        deletes.append(db_key)
                    else:
                        # note: we can break now, due to 'reverse=True' and txnums being big endian
                        break
                for key in deletes:
                    batch.delete(key)
            self.hist_db_tx_count = tx_count
            self.hist_db_tx_count_next = self.hist_db_tx_count
            self._write_state(batch)

        self.logger.info(f'backing up removed {nremoves:,d} history entries')

    def get_txnums(self, hashX: bytes, limit: int = 1000) -> Iterable[int]:
        '''Generator that returns an unpruned, sorted list of tx_nums in the
        history of a hashX.  Includes both spending and receiving
        transactions.  By default yields at most 1000 entries.  Set
        limit to None to get them all.  '''
        limit = util.resolve_limit(limit)
        prefix = b'H' + hashX
        for db_key, db_val in self.db.iterator(prefix=prefix):
            tx_numb = db_key[-TXNUM_LEN:]
            if limit == 0:
                return
            tx_num = unpack_txnum(tx_numb)
            yield tx_num
            limit -= 1
