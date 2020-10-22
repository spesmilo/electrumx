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
from typing import TYPE_CHECKING, Type, Optional

import electrumx.lib.util as util
from electrumx.lib.hash import HASHX_LEN, hash_to_hex_str
from electrumx.lib.util import (pack_be_uint16, pack_le_uint64,
                                unpack_be_uint16_from, unpack_le_uint64)

if TYPE_CHECKING:
    from electrumx.server.storage import Storage


TXNUM_LEN = 5


class History:

    DB_VERSIONS = (0, 1, 2)

    db: Optional['Storage']

    def __init__(self):
        self.logger = util.class_logger(__name__, self.__class__.__name__)
        self.unflushed = defaultdict(bytearray)
        self.unflushed_count = 0
        self.hist_db_tx_count = 0
        self.hist_db_tx_count_next = 0  # after next flush, next value for self.hist_db_tx_count
        self.db_version = max(self.DB_VERSIONS)
        self.upgrade_cursor = -1

        # Key: address_hashX + tx_num
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

    def close_db(self):
        if self.db:
            self.db.close()
            self.db = None

    def read_state(self):
        state = self.db.get(b'state\0\0')
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
            self.upgrade_db()
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

        key_len = HASHX_LEN + TXNUM_LEN
        txnum_padding = bytes(8-TXNUM_LEN)
        keys = []
        for db_key, db_val in self.db.iterator(prefix=b''):
            # Ignore non-history entries
            if len(db_key) != key_len:
                continue
            tx_numb = db_key[HASHX_LEN:]
            tx_num, = unpack_le_uint64(tx_numb + txnum_padding)
            if tx_num >= utxo_db_tx_count:
                keys.append(db_key)

        self.logger.info(f'deleting {len(keys):,d} history entries')

        self.hist_db_tx_count = utxo_db_tx_count
        self.hist_db_tx_count_next = self.hist_db_tx_count
        with self.db.write_batch() as batch:
            for key in keys:
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
        # History entries are not prefixed; the suffix \0\0 is just for legacy reasons
        batch.put(b'state\0\0', repr(state).encode())

    def add_unflushed(self, hashXs_by_tx, first_tx_num):
        unflushed = self.unflushed
        count = 0
        tx_num = None
        for tx_num, hashXs in enumerate(hashXs_by_tx, start=first_tx_num):
            tx_numb = pack_le_uint64(tx_num)[:TXNUM_LEN]
            hashXs = set(hashXs)
            for hashX in hashXs:
                unflushed[hashX] += tx_numb
            count += len(hashXs)
        self.unflushed_count += count
        if tx_num is not None:
            assert self.hist_db_tx_count_next + len(hashXs_by_tx) == tx_num + 1
            self.hist_db_tx_count_next = tx_num + 1

    def unflushed_memsize(self):
        return len(self.unflushed) * 180 + self.unflushed_count * TXNUM_LEN

    def assert_flushed(self):
        assert not self.unflushed

    def flush(self):
        start_time = time.monotonic()
        unflushed = self.unflushed
        chunks = util.chunks

        with self.db.write_batch() as batch:
            for hashX in sorted(unflushed):
                for tx_num in chunks(unflushed[hashX], TXNUM_LEN):
                    db_key = hashX + tx_num
                    batch.put(db_key, b'')
            self.hist_db_tx_count = self.hist_db_tx_count_next
            self.write_state(batch)

        count = len(unflushed)
        unflushed.clear()
        self.unflushed_count = 0

        if self.db.for_sync:
            elapsed = time.monotonic() - start_time
            self.logger.info(f'flushed history in {elapsed:.1f}s '
                             f'for {count:,d} addrs')

    def backup(self, hashXs, tx_count):
        self.assert_flushed()
        nremoves = 0
        txnum_padding = bytes(8-TXNUM_LEN)
        with self.db.write_batch() as batch:
            for hashX in sorted(hashXs):
                deletes = []
                for db_key, db_val in self.db.iterator(prefix=hashX, reverse=True):
                    tx_numb = db_key[HASHX_LEN:]
                    tx_num, = unpack_le_uint64(tx_numb + txnum_padding)
                    if tx_num >= tx_count:
                        nremoves += 1
                        deletes.append(db_key)
                    else:
                        break
                for key in deletes:
                    batch.delete(key)
            self.hist_db_tx_count = tx_count
            self.hist_db_tx_count_next = self.hist_db_tx_count
            self.write_state(batch)

        self.logger.info(f'backing up removed {nremoves:,d} history entries')

    def get_txnums(self, hashX, limit=1000):
        '''Generator that returns an unpruned, sorted list of tx_nums in the
        history of a hashX.  Includes both spending and receiving
        transactions.  By default yields at most 1000 entries.  Set
        limit to None to get them all.  '''
        limit = util.resolve_limit(limit)
        txnum_padding = bytes(8-TXNUM_LEN)
        for db_key, db_val in self.db.iterator(prefix=hashX):
            tx_numb = db_key[HASHX_LEN:]
            if limit == 0:
                return
            tx_num, = unpack_le_uint64(tx_numb + txnum_padding)
            yield tx_num
            limit -= 1

    #
    # DB upgrade
    #

    def upgrade_db(self):
        self.logger.info(f'history DB current version: {self.db_version}. '
                         f'latest is: {max(self.DB_VERSIONS)}')
        self.logger.info('Upgrading your history DB; this can take some time...')

        def convert_version_1():
            def upgrade_cursor(cursor):
                count = 0
                prefix = pack_be_uint16(cursor)
                key_len = HASHX_LEN + 2
                chunks = util.chunks
                with self.db.write_batch() as batch:
                    batch_put = batch.put
                    for key, hist in self.db.iterator(prefix=prefix):
                        # Ignore non-history entries
                        if len(key) != key_len:
                            continue
                        count += 1
                        hist = b''.join(item + b'\0' for item in chunks(hist, 4))
                        batch_put(key, hist)
                    self.upgrade_cursor = cursor
                    self.write_state(batch)
                return count

            last = time.monotonic()
            count = 0

            for cursor in range(self.upgrade_cursor + 1, 65536):
                count += upgrade_cursor(cursor)
                now = time.monotonic()
                if now > last + 10:
                    last = now
                    self.logger.info(f'history DB v0->v1: {count:,d} entries updated, '
                                     f'{cursor * 100 / 65536:.1f}% complete')

            self.db_version = 1
            self.upgrade_cursor = -1
            with self.db.write_batch() as batch:
                self.write_state(batch)
            self.logger.info('history DB upgraded to v1 successfully')

        def convert_version_2():
            # old schema:
            # Key: address_hashX + flush_id
            # Value: sorted "list" of tx_nums in history of hashX
            # -----
            # new schema:
            # Key: address_hashX + tx_num
            # Value: <null>

            def upgrade_cursor(cursor):
                count = 0
                prefix = pack_be_uint16(cursor)
                key_len = HASHX_LEN + 2
                chunks = util.chunks
                txnum_padding = bytes(8-TXNUM_LEN)
                with self.db.write_batch() as batch:
                    batch_put = batch.put
                    batch_delete = batch.delete
                    max_tx_num = 0
                    for db_key, db_val in self.db.iterator(prefix=prefix):
                        # Ignore non-history entries
                        if len(db_key) != key_len:
                            continue
                        count += 1
                        batch_delete(db_key)
                        hashX = db_key[:HASHX_LEN]
                        for tx_numb in chunks(db_val, 5):
                            batch_put(hashX + tx_numb, b'')
                            tx_num, = unpack_le_uint64(tx_numb + txnum_padding)
                            max_tx_num = max(max_tx_num, tx_num)
                    self.upgrade_cursor = cursor
                    self.hist_db_tx_count = max(self.hist_db_tx_count, max_tx_num + 1)
                    self.hist_db_tx_count_next = self.hist_db_tx_count
                    self.write_state(batch)
                return count

            last = time.monotonic()
            count = 0

            for cursor in range(self.upgrade_cursor + 1, 65536):
                count += upgrade_cursor(cursor)
                now = time.monotonic()
                if now > last + 10:
                    last = now
                    self.logger.info(f'history DB v1->v2: {count:,d} entries updated, '
                                     f'{cursor * 100 / 65536:.1f}% complete')

            self.db_version = 2
            self.upgrade_cursor = -1
            with self.db.write_batch() as batch:
                self.write_state(batch)
            self.logger.info('history DB upgraded to v2 successfully')

        if self.db_version == 0:
            convert_version_1()
        if self.db_version == 1:
            convert_version_2()
        self.db_version = max(self.DB_VERSIONS)
