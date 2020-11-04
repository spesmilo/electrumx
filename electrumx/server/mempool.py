# Copyright (c) 2016-2018, Neil Booth
#
# All rights reserved.
#
# See the file "LICENCE" for information about the copyright
# and warranty status of this software.

'''Mempool handling.'''

import itertools
import time
from abc import ABC, abstractmethod
from asyncio import Lock
from collections import defaultdict
from typing import Sequence, Tuple, TYPE_CHECKING, Type, Dict, Optional, Set
import math

import attr
from aiorpcx import TaskGroup, run_in_thread, sleep

from electrumx.lib.hash import hash_to_hex_str, hex_str_to_hash
from electrumx.lib.util import class_logger, chunks
from electrumx.server.db import UTXO

if TYPE_CHECKING:
    from electrumx.lib.coins import Coin


@attr.s(slots=True)
class MemPoolTx:
    prevouts = attr.ib()   # type: Sequence[Tuple[bytes, int]]  # (txid, txout_idx)
    in_pairs = attr.ib()   # type: Optional[Sequence[Tuple[bytes, int]]]  # (hashX, value_in_sats)
    out_pairs = attr.ib()  # type: Sequence[Tuple[bytes, int]]  # (hashX, value_in_sats)
    fee = attr.ib()        # type: int  # in sats
    size = attr.ib()       # type: int  # in vbytes


@attr.s(slots=True)
class MemPoolTxSummary:
    hash = attr.ib()
    fee = attr.ib()
    has_unconfirmed_inputs = attr.ib()


class DBSyncError(Exception):
    pass


class MemPoolAPI(ABC):
    '''A concrete instance of this class is passed to the MemPool object
    and used by it to query DB and blockchain state.'''

    @abstractmethod
    async def height(self) -> int:
        '''Query bitcoind for its height.'''

    @abstractmethod
    def cached_height(self) -> Optional[int]:
        '''Return the height of bitcoind the last time it was queried,
        for any reason, without actually querying it.
        '''

    @abstractmethod
    def db_height(self) -> int:
        '''Return the height flushed to the on-disk DB.'''

    @abstractmethod
    async def mempool_hashes(self):
        '''Query bitcoind for the hashes of all transactions in its
        mempool, returned as a list.'''

    @abstractmethod
    async def raw_transactions(self, hex_hashes):
        '''Query bitcoind for the serialized raw transactions with the given
        hashes.  Missing transactions are returned as None.

        hex_hashes is an iterable of hexadecimal hash strings.'''

    @abstractmethod
    async def lookup_utxos(self, prevouts):
        '''Return a list of (hashX, value) pairs, one for each prevout if unspent,
        otherwise return None if spent or not found (for the given prevout).

        prevouts - an iterable of (tx_hash, txout_idx) pairs
        '''

    @abstractmethod
    async def on_mempool(
            self,
            *,
            touched_hashxs: Set[bytes],
            touched_outpoints: Set[Tuple[bytes, int]],
            height: int,
    ):
        '''Called each time the mempool is synchronized.  touched_hashxs and
        touched_outpoints are sets of hashXs and tx outpoints touched since
        the previous call.  height is the  daemon's height at the time the
        mempool was obtained.
        '''


class MemPool:
    '''Representation of the daemon's mempool.

        coin - a coin class from coins.py
        api - an object implementing MemPoolAPI

    Updated regularly in caught-up state.  Goal is to enable efficient
    response to the calls in the external interface.  To that end we
    maintain the following maps:

       tx:     tx_hash -> MemPoolTx
       hashXs: hashX   -> set of all hashes of txs touching the hashX
    '''

    def __init__(self, coin: Type['Coin'], api: MemPoolAPI, refresh_secs=5.0, log_status_secs=60.0):
        assert isinstance(api, MemPoolAPI)
        self.coin = coin
        self.api = api
        self.logger = class_logger(__name__, self.__class__.__name__)
        self.txs = {}  # type: Dict[bytes, MemPoolTx]  # txid->tx
        self.hashXs = defaultdict(set)  # type: Dict[Optional[bytes], Set[bytes]]  # hashX->txids
        self.txo_to_spender = {}  # type: Dict[Tuple[bytes, int], bytes]  # prevout->txid
        self.cached_compact_histogram = []
        self.refresh_secs = refresh_secs
        self.log_status_secs = log_status_secs
        # Prevents mempool refreshes during fee histogram calculation
        self.lock = Lock()

    async def _logging(self, synchronized_event):
        '''Print regular logs of mempool stats.'''
        self.logger.info('beginning processing of daemon mempool.  '
                         'This can take some time...')
        start = time.monotonic()
        await synchronized_event.wait()
        elapsed = time.monotonic() - start
        self.logger.info(f'synced in {elapsed:.2f}s')
        while True:
            mempool_size = sum(tx.size for tx in self.txs.values()) / 1_000_000
            self.logger.info(f'{len(self.txs):,d} txs {mempool_size:.2f} MB, '
                             f'touching {len(self.hashXs):,d} addresses. '
                             f'{len(self.txo_to_spender):,d} spends.')
            await sleep(self.log_status_secs)
            await synchronized_event.wait()

    async def _refresh_histogram(self, synchronized_event):
        while True:
            await synchronized_event.wait()
            async with self.lock:
                # Threaded as can be expensive
                await run_in_thread(self._update_histogram, 100_000)
            await sleep(self.coin.MEMPOOL_HISTOGRAM_REFRESH_SECS)

    def _update_histogram(self, bin_size):
        # Build a histogram by fee rate
        histogram = defaultdict(int)
        for tx in self.txs.values():
            fee_rate = tx.fee / tx.size
            # use 0.1 sat/byte resolution
            # note: rounding *down* is intentional. This ensures txs
            #       with a given fee rate will end up counted in the expected
            #       bucket/interval of the compact histogram.
            fee_rate = math.floor(10 * fee_rate) / 10
            histogram[fee_rate] += tx.size

        compact = self._compress_histogram(histogram, bin_size=bin_size)
        self.logger.info(f'compact fee histogram: {compact}')
        self.cached_compact_histogram = compact

    @classmethod
    def _compress_histogram(
            cls, histogram: Dict[float, int], *, bin_size: int
    ) -> Sequence[Tuple[float, int]]:
        '''Calculate and return a compact fee histogram as needed for
        "mempool.get_fee_histogram" protocol request.

        histogram: feerate (sat/byte) -> total size in bytes of txs that pay approx feerate
        '''
        # Now compact it.  For efficiency, get_fees returns a
        # compact histogram with variable bin size.  The compact
        # histogram is an array of (fee_rate, vsize) values.
        # vsize_n is the cumulative virtual size of mempool
        # transactions with a fee rate in the interval
        # [rate_(n-1), rate_n)], and rate_(n-1) > rate_n.
        # Intervals are chosen to create tranches containing at
        # least 100kb of transactions
        assert bin_size > 0
        compact = []
        cum_size = 0
        prev_fee_rate = None
        for fee_rate, size in sorted(histogram.items(), reverse=True):
            # if there is a big lump of txns at this specific size,
            # consider adding the previous item now (if not added already)
            if size > 2 * bin_size and prev_fee_rate is not None and cum_size > 0:
                compact.append((prev_fee_rate, cum_size))
                cum_size = 0
                bin_size *= 1.1
            # now consider adding this item
            cum_size += size
            if cum_size > bin_size:
                compact.append((fee_rate, cum_size))
                cum_size = 0
                bin_size *= 1.1
            prev_fee_rate = fee_rate
        return compact

    def _accept_transactions(
            self,
            *,
            tx_map: Dict[bytes, MemPoolTx],  # txid->tx
            utxo_map: Dict[Tuple[bytes, int], Tuple[bytes, int]],  # prevout->(hashX,value_in_sats)
            touched_hashxs: Set[bytes],  # set of hashXs
            touched_outpoints: Set[Tuple[bytes, int]],  # set of outpoints
    ) -> Tuple[Dict[bytes, MemPoolTx],
               Dict[Tuple[bytes, int], Tuple[bytes, int]]]:
        '''Accept transactions in tx_map to the mempool if all their inputs
        can be found in the existing mempool or a utxo_map from the
        DB.

        Returns an (unprocessed tx_map, unspent utxo_map) pair.
        '''
        hashXs = self.hashXs
        txs = self.txs
        txo_to_spender = self.txo_to_spender

        deferred = {}
        unspent = set(utxo_map)
        # Try to find all prevouts so we can accept the TX
        for tx_hash, tx in tx_map.items():
            in_pairs = []
            try:
                for prevout in tx.prevouts:
                    utxo = utxo_map.get(prevout)
                    if not utxo:
                        prev_hash, prev_index = prevout
                        # Raises KeyError if prev_hash is not in txs
                        utxo = txs[prev_hash].out_pairs[prev_index]
                    in_pairs.append(utxo)
            except KeyError:
                deferred[tx_hash] = tx
                continue

            # Spend the prevouts
            unspent.difference_update(tx.prevouts)

            # Save the in_pairs, compute the fee and accept the TX
            tx.in_pairs = tuple(in_pairs)
            # Avoid negative fees if dealing with generation-like transactions
            # because some in_parts would be missing
            tx.fee = max(0, (sum(v for _, v in tx.in_pairs) -
                             sum(v for _, v in tx.out_pairs)))
            txs[tx_hash] = tx

            for hashX, _value in itertools.chain(tx.in_pairs, tx.out_pairs):
                touched_hashxs.add(hashX)
                hashXs[hashX].add(tx_hash)
            for prevout in tx.prevouts:
                txo_to_spender[prevout] = tx_hash
                touched_outpoints.add(prevout)
            for out_idx, out_pair in enumerate(tx.out_pairs):
                touched_outpoints.add((tx_hash, out_idx))

        return deferred, {prevout: utxo_map[prevout] for prevout in unspent}

    async def _refresh_hashes(self, synchronized_event):
        '''Refresh our view of the daemon's mempool.'''
        # touched_* accumulates between calls to on_mempool and each
        # call transfers ownership
        touched_hashxs = set()
        touched_outpoints = set()
        while True:
            height = self.api.cached_height()
            hex_hashes = await self.api.mempool_hashes()
            if height != await self.api.height():
                continue
            hashes = {hex_str_to_hash(hh) for hh in hex_hashes}
            try:
                async with self.lock:
                    await self._process_mempool(
                        all_hashes=hashes,
                        touched_hashxs=touched_hashxs,
                        touched_outpoints=touched_outpoints,
                        mempool_height=height,
                    )
            except DBSyncError:
                # The UTXO DB is not at the same height as the
                # mempool; wait and try again
                self.logger.debug('waiting for DB to sync')
            else:
                synchronized_event.set()
                synchronized_event.clear()
                await self.api.on_mempool(
                    touched_hashxs=touched_hashxs,
                    touched_outpoints=touched_outpoints,
                    height=height,
                )
                touched_hashxs = set()
                touched_outpoints = set()
            await sleep(self.refresh_secs)

    async def _process_mempool(
            self,
            *,
            all_hashes: Set[bytes],  # set of txids
            touched_hashxs: Set[bytes],  # set of hashXs
            touched_outpoints: Set[Tuple[bytes, int]],  # set of outpoints
            mempool_height: int,
    ) -> None:
        # Re-sync with the new set of hashes
        txs = self.txs
        hashXs = self.hashXs
        txo_to_spender = self.txo_to_spender

        if mempool_height != self.api.db_height():
            raise DBSyncError

        # First handle txs that have disappeared
        for tx_hash in (set(txs) - all_hashes):
            tx = txs.pop(tx_hash)
            # hashXs
            tx_hashXs = {hashX for hashX, value in tx.in_pairs}
            tx_hashXs.update(hashX for hashX, value in tx.out_pairs)
            for hashX in tx_hashXs:
                hashXs[hashX].remove(tx_hash)
                if not hashXs[hashX]:
                    del hashXs[hashX]
            touched_hashxs |= tx_hashXs
            # outpoints
            for prevout in tx.prevouts:
                del txo_to_spender[prevout]
                touched_outpoints.add(prevout)
            for out_idx, out_pair in enumerate(tx.out_pairs):
                touched_outpoints.add((tx_hash, out_idx))

        # Process new transactions
        new_hashes = list(all_hashes.difference(txs))
        if new_hashes:
            group = TaskGroup()
            for hashes in chunks(new_hashes, 200):
                coro = self._fetch_and_accept(
                    hashes=hashes,
                    all_hashes=all_hashes,
                    touched_hashxs=touched_hashxs,
                    touched_outpoints=touched_outpoints,
                )
                await group.spawn(coro)
            if mempool_height != self.api.db_height():
                raise DBSyncError

            tx_map = {}
            utxo_map = {}
            async for task in group:
                deferred, unspent = task.result()
                tx_map.update(deferred)
                utxo_map.update(unspent)

            prior_count = 0
            # FIXME: this is not particularly efficient
            while tx_map and len(tx_map) != prior_count:
                prior_count = len(tx_map)
                tx_map, utxo_map = self._accept_transactions(
                    tx_map=tx_map,
                    utxo_map=utxo_map,
                    touched_hashxs=touched_hashxs,
                    touched_outpoints=touched_outpoints,
                )
            if tx_map:
                self.logger.error(f'{len(tx_map)} txs dropped')

    async def _fetch_and_accept(
            self,
            *,
            hashes: Set[bytes],  # set of txids
            all_hashes: Set[bytes],  # set of txids
            touched_hashxs: Set[bytes],  # set of hashXs
            touched_outpoints: Set[Tuple[bytes, int]],  # set of outpoints
    ):
        '''Fetch a list of mempool transactions.'''
        hex_hashes_iter = (hash_to_hex_str(hash) for hash in hashes)
        raw_txs = await self.api.raw_transactions(hex_hashes_iter)

        def deserialize_txs():    # This function is pure
            to_hashX = self.coin.hashX_from_script
            deserializer = self.coin.DESERIALIZER

            txs = {}
            for hash, raw_tx in zip(hashes, raw_txs):
                # The daemon may have evicted the tx from its
                # mempool or it may have gotten in a block
                if not raw_tx:
                    continue
                tx, tx_size = deserializer(raw_tx).read_tx_and_vsize()
                # Convert the inputs and outputs into (hashX, value) pairs
                # Drop generation-like inputs from MemPoolTx.prevouts
                txin_pairs = tuple((txin.prev_hash, txin.prev_idx)
                                   for txin in tx.inputs
                                   if not txin.is_generation())
                txout_pairs = tuple((to_hashX(txout.pk_script), txout.value)
                                    for txout in tx.outputs)
                txs[hash] = MemPoolTx(
                    prevouts=txin_pairs,
                    in_pairs=None,
                    out_pairs=txout_pairs,
                    fee=0,
                    size=tx_size,
                )
            return txs

        # Thread this potentially slow operation so as not to block
        tx_map = await run_in_thread(deserialize_txs)

        # Determine all prevouts not in the mempool, and fetch the
        # UTXO information from the database.  Failed prevout lookups
        # return None - concurrent database updates happen - which is
        # relied upon by _accept_transactions. Ignore prevouts that are
        # generation-like.
        prevouts = tuple(prevout for tx in tx_map.values()
                         for prevout in tx.prevouts
                         if prevout[0] not in all_hashes)
        utxos = await self.api.lookup_utxos(prevouts)
        utxo_map = {prevout: utxo for prevout, utxo in zip(prevouts, utxos)}

        return self._accept_transactions(
            tx_map=tx_map,
            utxo_map=utxo_map,
            touched_hashxs=touched_hashxs,
            touched_outpoints=touched_outpoints,
        )

    #
    # External interface
    #

    async def keep_synchronized(self, synchronized_event):
        '''Keep the mempool synchronized with the daemon.'''
        async with TaskGroup() as group:
            await group.spawn(self._refresh_hashes(synchronized_event))
            await group.spawn(self._refresh_histogram(synchronized_event))
            await group.spawn(self._logging(synchronized_event))

    async def balance_delta(self, hashX):
        '''Return the unconfirmed amount in the mempool for hashX.

        Can be positive or negative.
        '''
        value = 0
        if hashX in self.hashXs:
            for hash in self.hashXs[hashX]:
                tx = self.txs[hash]
                value -= sum(v for h168, v in tx.in_pairs if h168 == hashX)
                value += sum(v for h168, v in tx.out_pairs if h168 == hashX)
        return value

    async def compact_fee_histogram(self):
        '''Return a compact fee histogram of the current mempool.'''
        return self.cached_compact_histogram

    async def potential_spends(self, hashX):
        '''Return a set of (prev_hash, prev_idx) pairs from mempool
        transactions that touch hashX.

        None, some or all of these may be spends of the hashX, but all
        actual spends of it (in the DB or mempool) will be included.
        '''
        result = set()
        for tx_hash in self.hashXs.get(hashX, ()):
            tx = self.txs[tx_hash]
            result.update(tx.prevouts)
        return result

    async def transaction_summaries(self, hashX):
        '''Return a list of MemPoolTxSummary objects for the hashX.'''
        result = []
        for tx_hash in self.hashXs.get(hashX, ()):
            tx = self.txs[tx_hash]
            has_ui = any(hash in self.txs for hash, idx in tx.prevouts)
            result.append(MemPoolTxSummary(tx_hash, tx.fee, has_ui))
        return result

    async def unordered_UTXOs(self, hashX):
        '''Return an unordered list of UTXO named tuples from mempool
        transactions that pay to hashX.

        This does not consider if any other mempool transactions spend
        the outputs.
        '''
        utxos = []
        for tx_hash in self.hashXs.get(hashX, ()):
            tx = self.txs.get(tx_hash)
            for pos, (hX, value) in enumerate(tx.out_pairs):
                if hX == hashX:
                    utxos.append(UTXO(-1, pos, tx_hash, 0, value))
        return utxos

    def spender_for_txo(self, prev_txhash: bytes, txout_idx: int) -> Optional[bytes]:
        '''For a prevout, returns the txid that spent it.

        This only considers spenders in the mempool, i.e. if there is a tx in
        the mempool that spends prevout, return its txid, or None otherwise.
        '''
        prevout = (prev_txhash, txout_idx)
        return self.txo_to_spender.get(prevout, None)

    def txo_exists_in_mempool(self, tx_hash: bytes, txout_idx: int) -> bool:
        '''For an outpoint, returns whether a mempool tx created it,
        regardless of whether it has been spent.
        '''
        tx = self.txs.get(tx_hash, None)
        if tx is None:
            return False
        return len(tx.out_pairs) > txout_idx
