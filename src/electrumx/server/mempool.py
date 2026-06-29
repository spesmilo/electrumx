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
import asyncio
from asyncio import Lock
from collections import defaultdict
from dataclasses import dataclass
from typing import Sequence, Tuple, TYPE_CHECKING, Type, Dict, Optional, Set, Iterable
import math
from graphlib import TopologicalSorter

from aiorpcx import run_in_thread, sleep, ignore_after

from electrumx.lib.hash import hash_to_hex_str, hex_str_to_hash
from electrumx.lib.tx import SkipTxDeserialize
from electrumx.lib.util import class_logger, chunks, OldTaskGroup
from electrumx.lib.tx import TXOSpendStatus, TxOutpoint
from electrumx.server.db import UTXO

if TYPE_CHECKING:
    from electrumx.lib.coins import Coin


DB_UTXO_MAP = dict[TxOutpoint, Optional[tuple[bytes, int]]]  # prevout->(hashX,value_in_sats)


@dataclass(slots=True)
class MemPoolTx:
    prevouts: Sequence[TxOutpoint]  # (txid_rev, txout_idx)
    # A pair is a (hashX, value) tuple
    in_pairs: Optional[Sequence[tuple[bytes, int]]]  # (hashX, value_in_sats)
    out_pairs: Sequence[tuple[bytes, int]]  # (hashX, value_in_sats)
    fee: int  # in sats
    size: int  # in vbytes


@dataclass(slots=True)
class MemPoolTxSummary:
    txid_rev: bytes
    fee: int  # in sats
    has_unconfirmed_inputs: bool


@dataclass(slots=True, frozen=True, kw_only=True)
class RecentMemPoolTx:
    txid_rev: bytes
    fee: int         # in sats
    vsize: int       # in vbytes


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
    async def db_height_changed(self) -> None:
        '''Wait until the on-disk DB height changes.'''

    @abstractmethod
    async def mempool_txids_hum(self) -> Sequence[str]:
        '''Query bitcoind for the txids of all transactions in its
        mempool, returned as a list.'''

    @abstractmethod
    async def raw_transactions(self, txids_hum: Iterable[str]) -> Sequence[bytes | None]:
        '''Query bitcoind for the serialized raw transactions with the given
        txids.  Missing transactions are returned as None.

        txids_hum is an iterable of hexadecimal hash strings.'''

    @abstractmethod
    async def lookup_utxos(self, prevouts: Sequence[TxOutpoint]) -> Sequence[Optional[Tuple[bytes, int]]]:
        '''Return a list of (hashX, value) pairs, one for each prevout if unspent,
        otherwise return None if spent or not found (for the given prevout).

        prevouts - an iterable of (txid_rev, txout_idx) pairs
        '''

    @abstractmethod
    async def on_mempool(
            self,
            *,
            touched_hashxs: Set[bytes],
            touched_outpoints: Set[TxOutpoint],
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

       tx:     txid_rev -> MemPoolTx
       hashXs: hashX   -> set of all txids_rev of txs touching the hashX
    '''

    def __init__(
            self,
            coin: Type['Coin'],
            api: MemPoolAPI,
            *,
            refresh_secs=5.0,
            log_status_secs=60.0,
    ):
        assert isinstance(api, MemPoolAPI)
        self.coin = coin
        self.api = api
        self.logger = class_logger(__name__, self.__class__.__name__)
        self.txs = {}  # type: Dict[bytes, MemPoolTx]  # txid_rev->tx
        self.hashXs = defaultdict(set)  # type: Dict[Optional[bytes], Set[bytes]]  # hashX->txids_rev
        self.txo_to_spender = {}  # type: Dict[TxOutpoint, bytes]  # prevout->txid_rev
        self.cached_compact_histogram = []  # type: Sequence[tuple[float, int]]
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
                bin_size = self.coin.MEMPOOL_COMPACT_HISTOGRAM_BINSIZE
                await run_in_thread(self._update_histogram, bin_size)
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

        histogram: feerate (sat/vbyte) -> total size in bytes of txs that pay approx feerate
        bin_size: ~minimum vsize of a bucket of txs in the result (e.g. 100 kb)
        '''
        # Now compact it.  For efficiency, get_fees returns a
        # compact histogram with variable bin size.  The compact
        # histogram is an array of (fee_rate, vsize) values.
        # vsize_n is the cumulative virtual size of mempool
        # transactions with a fee rate in the interval
        # [rate_(n-1), rate_n)], and rate_(n-1) > rate_n.
        # Intervals are chosen to create tranches containing at
        # least a certain cumulative size (bin_size) of transactions.
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
            tx_map: Dict[bytes, MemPoolTx],  # txid_rev->tx
            utxo_map: DB_UTXO_MAP,  # prevout->(hashX,value_in_sats)
            touched_hashxs: Set[bytes],  # set of hashXs
            touched_outpoints: Set[TxOutpoint],  # set of outpoints
            topologically_sort: bool,
    ) -> tuple[dict[bytes, MemPoolTx], DB_UTXO_MAP]:
        '''Accept transactions in tx_map to the mempool if all their inputs
        can be found in the existing mempool or a utxo_map from the
        DB.

        Returns an (unprocessed tx_map, unspent utxo_map) pair.
        '''
        hashXs = self.hashXs
        txs = self.txs
        txo_to_spender = self.txo_to_spender

        if topologically_sort:
            # Sort tx_map so that parent txs come first (in case of unconf chain).
            # This is just an optimization so that fewer txs will get "deferred", leading to better perf.
            # Dependencies outside tx_map (already in chainstate (utxo_map), or our mempool (self.txs),
            # or MISSING) are ignored.
            cand_to_parents = {
                txid: {parent_txid for (parent_txid, txout_idx) in tx.prevouts
                       if parent_txid in tx_map}
                for (txid, tx) in tx_map.items()}
            cand_txids = list(TopologicalSorter(cand_to_parents).static_order())
            assert len(tx_map) == len(cand_to_parents) == len(cand_txids), \
                f"{len(tx_map)=}, {len(cand_to_parents)=}, {len(cand_txids)=}"
            del cand_to_parents
        else:
            cand_txids = tx_map.keys()

        deferred = {}  # type: dict[bytes, MemPoolTx]
        unspent = set(utxo_map)
        # Try to find all prevouts so we can accept the candidate TXs from tx_map into our mempool
        for txid_rev in cand_txids:
            tx = tx_map[txid_rev]
            in_pairs = []
            try:
                for prevout in tx.prevouts:
                    # first, look for parent tx among confirmed UTXOs:
                    utxo = utxo_map.get(prevout)
                    if not utxo:  # second, look for parent tx in mempool
                        prev_hash, prev_index = prevout
                        # Raises KeyError if prev_hash is not in txs
                        utxo = txs[prev_hash].out_pairs[prev_index]
                    in_pairs.append(utxo)
            except KeyError:
                deferred[txid_rev] = tx
                continue

            # Spend the prevouts
            unspent.difference_update(tx.prevouts)

            # Save the in_pairs, compute the fee and accept the TX
            tx.in_pairs = tuple(in_pairs)
            # Avoid negative fees if dealing with generation-like transactions
            # because some in_parts would be missing
            tx.fee = max(0, (sum(v for _, v in tx.in_pairs) -
                             sum(v for _, v in tx.out_pairs)))
            txs[txid_rev] = tx

            for hashX, _value in itertools.chain(tx.in_pairs, tx.out_pairs):
                touched_hashxs.add(hashX)
                hashXs[hashX].add(txid_rev)
            for prevout in tx.prevouts:
                txo_to_spender[prevout] = txid_rev
                touched_outpoints.add(prevout)
            for out_idx, out_pair in enumerate(tx.out_pairs):
                touched_outpoints.add((txid_rev, out_idx))

        return deferred, {prevout: utxo_map[prevout] for prevout in unspent}

    async def _refresh_hashes(self, synchronized_event):
        '''Refresh our view of the daemon's mempool.'''
        # touched_* accumulates between calls to on_mempool and each
        # call transfers ownership
        touched_hashxs = set()
        touched_outpoints = set()
        while True:
            height = self.api.cached_height()
            txids_hum = await self.api.mempool_txids_hum()
            if height != await self.api.height():  # if height changed *again*, re-start
                continue
            txids_rev = await run_in_thread(lambda: {hex_str_to_hash(hh) for hh in txids_hum})
            try:
                async with self.lock:
                    await self._process_mempool(
                        all_txids_rev=txids_rev,
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
            # poll bitcoind's whole mempool every few seconds; or instantly after the DB height changes
            async with ignore_after(self.refresh_secs):
                await self.api.db_height_changed()

    async def _process_mempool(
            self,
            *,
            all_txids_rev: Set[bytes],  # set of txids_rev  # complete view of daemon's mempool
            touched_hashxs: Set[bytes],  # set of hashXs
            touched_outpoints: Set[TxOutpoint],  # set of outpoints
            mempool_height: int,
    ) -> None:
        # Re-sync with the new set of hashes
        txs = self.txs
        hashXs = self.hashXs
        txo_to_spender = self.txo_to_spender

        if mempool_height != self.api.db_height():  # FIXME should compare blockhash
            raise DBSyncError

        # 1. Handle txs that have disappeared (evicted, just got mined, etc)
        # TODO split disappeared txs workload into a threadpool, chunks of ~200 txs
        def handle_disappeared_txs() -> int:
            nonlocal touched_hashxs
            disappeared_hashes = set(txs) - all_txids_rev
            for txid_rev in disappeared_hashes:
                tx = txs.pop(txid_rev)
                # hashXs
                tx_hashXs = {hashX for hashX, value in tx.in_pairs}
                tx_hashXs.update(hashX for hashX, value in tx.out_pairs)
                for hashX in tx_hashXs:
                    hashXs[hashX].remove(txid_rev)
                    if not hashXs[hashX]:
                        del hashXs[hashX]
                touched_hashxs |= tx_hashXs
                # outpoints
                for prevout in tx.prevouts:
                    del txo_to_spender[prevout]
                    touched_outpoints.add(prevout)
                for out_idx, out_pair in enumerate(tx.out_pairs):
                    touched_outpoints.add((txid_rev, out_idx))
            return len(disappeared_hashes)

        await run_in_thread(handle_disappeared_txs)

        # 2. Process new transactions
        new_hashes = await run_in_thread(lambda: list(all_txids_rev.difference(txs)))
        if new_hashes:
            # 2.1. fetch raw txs from bitcoin daemon
            group = OldTaskGroup()
            for hashes in chunks(new_hashes, 200):
                coro = self._fetch_raw_txs_and_utxos(
                    new_txids_rev=hashes,
                    all_txids_rev=all_txids_rev,
                )
                await group.spawn(coro)
            if mempool_height != self.api.db_height():
                raise DBSyncError

            tx_map = {}  # type: dict[bytes, MemPoolTx]
            utxo_map = {}  # type: DB_UTXO_MAP
            async for task in group:
                partial_tx_map, partial_utxo_map = task.result()
                tx_map.update(partial_tx_map)
                utxo_map.update(partial_utxo_map)

            # 2.2. accept txs into our mempool
            def accept_txs_loop() -> None:
                # Accept candidate txs from tx_map into our mempool.
                # We only accept candidates for which we can find a UTXO for each tx input:
                # - some UTXOs we find in the DB=utxo_map (chainstate),
                # - some we find in self.txs (our memool),
                # - some we might only find in tx_map (among the candidates: consider long chain of unconfirmed txs)
                # - some we might not find anywhere: if DB is corrupted or bitcoind gave inconsistent mempool, or other race
                # In each iteration, we loop over tx_map and move accepted transactions from it to self.txs.
                # In the worst degenerate case, this could be O(n^2). To avoid that, we topologically sort tx_map.
                nonlocal tx_map, utxo_map
                prior_txmap_size = 0
                while tx_map and len(tx_map) != prior_txmap_size:
                    prior_txmap_size = len(tx_map)
                    tx_map, utxo_map = self._accept_transactions(
                        tx_map=tx_map,
                        utxo_map=utxo_map,
                        touched_hashxs=touched_hashxs,
                        touched_outpoints=touched_outpoints,
                        topologically_sort=True,
                    )
                if tx_map:
                    self.logger.error(f'{len(tx_map)} txs dropped')

            await run_in_thread(accept_txs_loop)

    async def _fetch_raw_txs_and_utxos(
            self,
            *,
            new_txids_rev: set[bytes],  # (some) new candidate txs for our mempool
            all_txids_rev: set[bytes],  # complete view of daemon's mempool
    ) -> tuple[dict[bytes, MemPoolTx], DB_UTXO_MAP]:
        '''Fetch a list of mempool transactions, and lookup corresponding UTXOs in the DB.'''
        txids_hum_iter = (hash_to_hex_str(hash) for hash in new_txids_rev)
        raw_txs = await self.api.raw_transactions(txids_hum_iter)

        def deserialize_txs() -> Dict[bytes, MemPoolTx]:
            """This function is pure"""
            to_hashX = self.coin.hashX_from_script
            deserializer = self.coin.DESERIALIZER

            txs = {}  # type: Dict[bytes, MemPoolTx]
            for txid_rev, raw_tx in zip(new_txids_rev, raw_txs):
                # The daemon may have evicted the tx from its
                # mempool or it may have gotten in a block
                if not raw_tx:
                    continue
                try:
                    tx, tx_size = deserializer(raw_tx).read_tx_and_vsize()
                except SkipTxDeserialize as ex:
                    self.logger.debug(f'skipping tx {hash_to_hex_str(txid_rev)}: {ex}')
                    continue
                # Convert the inputs and outputs into (hashX, value) pairs
                # Drop generation-like inputs from MemPoolTx.prevouts
                txin_pairs = tuple((txin.prev_txid_rev, txin.prev_idx)
                                   for txin in tx.inputs
                                   if not txin.is_generation())
                txout_pairs = tuple((to_hashX(txout.pk_script), txout.value)
                                    for txout in tx.outputs)
                txs[txid_rev] = MemPoolTx(
                    prevouts=txin_pairs,
                    in_pairs=None,
                    out_pairs=txout_pairs,
                    fee=0,
                    size=tx_size,
                )
            return txs

        # Thread this potentially slow operation so as not to block
        tx_map = await run_in_thread(deserialize_txs)  # type: Dict[bytes, MemPoolTx]

        # Determine all prevouts not in the mempool, and fetch the
        # UTXO information from the database.  Failed prevout lookups
        # return None - concurrent database updates happen - which is
        # relied upon by _accept_transactions. Ignore prevouts that are
        # generation-like.
        prevouts = tuple(prevout for tx in tx_map.values()
                         for prevout in tx.prevouts
                         if prevout[0] not in all_txids_rev)
        utxos = await self.api.lookup_utxos(prevouts)
        utxo_map = {prevout: utxo for prevout, utxo in zip(prevouts, utxos)}

        return tx_map, utxo_map

    #
    # External interface
    #

    async def keep_synchronized(self, synchronized_event: asyncio.Event) -> None:
        '''Keep the mempool synchronized with the daemon.'''
        async with OldTaskGroup() as group:
            await group.spawn(self._refresh_hashes(synchronized_event))
            await group.spawn(self._refresh_histogram(synchronized_event))
            await group.spawn(self._logging(synchronized_event))

    async def balance_delta(self, hashX: bytes) -> int:
        '''Return the unconfirmed amount in the mempool for hashX.

        Can be positive or negative.
        '''
        value = 0
        if hashX in self.hashXs:
            for txid_rev in self.hashXs[hashX]:
                tx = self.txs[txid_rev]
                value -= sum(v for h168, v in tx.in_pairs if h168 == hashX)
                value += sum(v for h168, v in tx.out_pairs if h168 == hashX)
        return value

    async def compact_fee_histogram(self) -> Sequence[tuple[float, int]]:
        '''Return a compact fee histogram of the current mempool.'''
        return self.cached_compact_histogram

    async def potential_spends(self, hashX: bytes) -> set[TxOutpoint]:
        '''Return a set of (prev_hash, prev_idx) pairs from mempool
        transactions that touch hashX.

        None, some or all of these may be spends of the hashX, but all
        actual spends of it (in the DB or mempool) will be included.
        '''
        result = set()
        for txid_rev in self.hashXs.get(hashX, ()):
            tx = self.txs[txid_rev]
            result.update(tx.prevouts)
        return result

    async def transaction_summaries(self, hashX: bytes) -> Sequence[MemPoolTxSummary]:
        '''Return a list of MemPoolTxSummary objects for the hashX,
        sorted as expected by protocol methods.
        '''
        result = []  # type: list[MemPoolTxSummary]
        for txid_rev in self.hashXs.get(hashX, ()):
            tx = self.txs[txid_rev]
            has_ui = any(hash in self.txs for hash, idx in tx.prevouts)
            result.append(MemPoolTxSummary(txid_rev, tx.fee, has_ui))
        result.sort(key=lambda x: (x.has_unconfirmed_inputs, x.txid_rev[::-1]))
        return result

    async def unordered_UTXOs(self, hashX: bytes) -> Sequence[UTXO]:
        '''Return an unordered list of UTXO named tuples from mempool
        transactions that pay to hashX.

        This does not consider if any other mempool transactions spend
        the outputs.
        '''
        utxos = []
        for txid_rev in self.hashXs.get(hashX, ()):
            tx = self.txs.get(txid_rev)
            for pos, (hX, value) in enumerate(tx.out_pairs):
                if hX == hashX:
                    utxos.append(UTXO(-1, pos, txid_rev, 0, value))
        return utxos

    async def spender_for_txo(self, prev_txid_rev: bytes, txout_idx: int) -> 'TXOSpendStatus':
        '''For an outpoint, returns its spend-status.
        This only considers the mempool, not the DB/blockchain, so e.g. mined
        txs are not distinguished from txs that never existed.
        '''
        # look up funding tx
        prev_tx = self.txs.get(prev_txid_rev, None)
        if prev_tx is None:
            # funding tx already mined or never existed
            funder_height = None
        else:
            if len(prev_tx.out_pairs) <= txout_idx:
                # output idx out of bounds...?
                return TXOSpendStatus(funder_height=None)
            prev_has_ui = any(hash in self.txs for hash, idx in prev_tx.prevouts)
            funder_height = -prev_has_ui
        prevout = (prev_txid_rev, txout_idx)
        # look up spending tx
        spender_txid_rev = self.txo_to_spender.get(prevout, None)
        if spender_txid_rev is None:
            return TXOSpendStatus(funder_height=funder_height)
        spender_tx = self.txs.get(spender_txid_rev, None)
        if spender_tx is None:
            self.logger.warning(f"spender_tx {hash_to_hex_str(spender_txid_rev)} not in"
                                f"mempool, but txo_to_spender referenced it as spender "
                                f"of {hash_to_hex_str(prev_txid_rev)}:{txout_idx} ?!")
            return TXOSpendStatus(funder_height=funder_height)
        spender_has_ui = any(txid_rev in self.txs for txid_rev, idx in spender_tx.prevouts)
        spender_height = -spender_has_ui
        return TXOSpendStatus(
            funder_height=funder_height,
            spender_txid_rev=spender_txid_rev,
            spender_height=spender_height,
        )

    async def get_recently_added_txs(self, *, count: int) -> Sequence[RecentMemPoolTx]:
        # note: inefficient for large "count"s
        it = reversed(self.txs.items())
        count = min(count, len(self.txs))
        mempool_txs = [next(it) for _ in range(count)]
        return [
            RecentMemPoolTx(txid_rev=hash, fee=mtx.fee, vsize=mtx.size)
            for hash, mtx in mempool_txs]
