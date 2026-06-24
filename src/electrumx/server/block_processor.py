# Copyright (c) 2016-2017, Neil Booth
# Copyright (c) 2017, the ElectrumX authors
#
# All rights reserved.
#
# See the file "LICENCE" for information about the copyright
# and warranty status of this software.

'''Block prefetcher and chain processor.'''


import asyncio
import concurrent.futures
from concurrent.futures import ThreadPoolExecutor
from functools import partial
import sys
import time
from typing import Sequence, Tuple, List, Callable, Optional, TYPE_CHECKING, Type, Set

from aiorpcx import run_in_thread, CancelledError, ignore_after

import electrumx
from electrumx.server.daemon import DaemonError, Daemon
from electrumx.lib.hash import hash_to_hex_str, HASHX_LEN
from electrumx.lib.script import is_unspendable_legacy, is_unspendable_genesis
from electrumx.lib.util import (
    chunks, class_logger, OldTaskGroup,
)
from electrumx.lib.tx import Tx
from electrumx.server.db import FlushData, COMP_TXID_LEN, DB
from electrumx.server.db_util import (
    pack_txnum, unpack_txnum, TXNUM_LEN,
    pack_txoutidx, unpack_txoutidx, TXOUTIDX_LEN,
    pack_satoshis_val, unpack_satoshis_val,
)

if TYPE_CHECKING:
    from electrumx.lib.coins import Coin, Block
    from electrumx.server.env import Env
    from electrumx.server.controller import Notifications


class Prefetcher:
    '''Prefetches blocks (in the forward direction only).'''

    def __init__(
            self,
            daemon: 'Daemon',
            coin: Type['Coin'],
            blocks_event: asyncio.Event,
            *,
            polling_delay_secs: float,
    ):
        self.logger = class_logger(__name__, self.__class__.__name__)
        self.daemon = daemon
        self.coin = coin
        self.blocks_event = blocks_event
        self.blocks = []  # type: List[bytes]
        self.caught_up = False
        # Access to fetched_height should be protected by the semaphore
        self.fetched_height = None
        self.semaphore = asyncio.Semaphore()
        self.refill_event = asyncio.Event()
        # The prefetched block cache size.  The min cache size has
        # little effect on sync time.
        self.cache_size = 0
        self.min_cache_size = 10 * 1024 * 1024
        # This makes the first fetch be 10 blocks
        self.ave_size = max(1, self.min_cache_size // 10)
        self.polling_delay = polling_delay_secs

    async def main_loop(self, bp_height: int) -> None:
        '''Loop forever polling for more blocks.'''
        await self.reset_height(bp_height)
        while True:
            try:
                # Sleep a while if there is nothing to prefetch
                await self.refill_event.wait()
                daemon_is_ahead = await self._prefetch_blocks()
                if not daemon_is_ahead:
                    # The mempool logic and maybe others can independently notice the daemon's height
                    # changing. If that happens, we should wake up immediately to fetch blocks.
                    async with ignore_after(self.polling_delay):
                        await self.daemon.height_changed.wait()
            except DaemonError as e:
                self.logger.info(f'ignoring daemon error: {e}')
            except asyncio.CancelledError as e:
                self.logger.info(f'cancelled; prefetcher stopping {e}')
                raise
            except Exception:
                self.logger.exception(f'ignoring unexpected exception')

    def get_prefetched_blocks(self) -> Sequence[bytes]:
        '''Called by block processor when it is processing queued blocks.'''
        blocks = self.blocks
        self.blocks = []
        self.cache_size = 0
        self.refill_event.set()
        return blocks

    async def reset_height(self, height: int) -> None:
        '''Reset to prefetch blocks from the block processor's height.

        Used in blockchain reorganisations.  This coroutine can be
        called asynchronously to the _prefetch_blocks coroutine so we
        must synchronize with a semaphore.
        '''
        async with self.semaphore:
            self.blocks.clear()
            self.cache_size = 0
            self.fetched_height = height
            self.refill_event.set()

        daemon_height = await self.daemon.height()
        behind = daemon_height - height
        if behind > 0:
            self.logger.info(
                f'catching up to daemon height {daemon_height:,d} ({behind:,d} '
                f'blocks behind)'
            )
        else:
            self.logger.info(f'caught up to daemon height {daemon_height:,d}')

    async def _prefetch_blocks(self) -> bool:
        '''Prefetch some blocks and put them on the queue.

        Repeats until the queue is full or caught up.
        '''
        daemon = self.daemon
        daemon_height = await daemon.height()
        async with self.semaphore:
            while self.cache_size < self.min_cache_size:
                first = self.fetched_height + 1
                # Try and catch up all blocks but limit to room in cache.
                cache_room = max(self.min_cache_size // self.ave_size, 1)
                count = min(daemon_height - self.fetched_height, cache_room)
                # Don't make too large a request
                count = min(self.coin.max_fetch_blocks(first), max(count, 0))
                if not count:
                    self.caught_up = True
                    return False

                hex_hashes = await daemon.block_hex_hashes(first, count)
                if self.caught_up:
                    self.logger.info(f'new block height {first + count-1:,d} '
                                     f'hash {hex_hashes[-1]}')
                blocks = await daemon.raw_blocks(hex_hashes)

                assert count == len(blocks)

                # Special handling for genesis block
                if first == 0:
                    blocks[0] = self.coin.genesis_block(blocks[0])
                    self.logger.info(f'verified genesis block with hash '
                                     f'{hex_hashes[0]}')

                # Update our recent average block size estimate
                size = sum(len(block) for block in blocks)
                if count >= 10:
                    self.ave_size = size // count
                else:
                    self.ave_size = (size + (10 - count) * self.ave_size) // 10

                self.blocks.extend(blocks)
                self.cache_size += size
                self.fetched_height += count
                self.blocks_event.set()

        self.refill_event.clear()
        return True


class ChainError(Exception):
    '''Raised on error processing blocks.'''


class BlockProcessor:
    '''Process blocks and update the DB state to match.

    Employ a prefetcher to prefetch blocks in batches for processing.
    Coordinate backing up in case of chain reorganisations.
    '''

    def __init__(self, env: 'Env', db: DB, daemon: Daemon, notifications: 'Notifications'):
        self.env = env
        self.db = db
        self.daemon = daemon
        self.notifications = notifications

        self.coin = env.coin
        # blocks_event: set when new blocks are put on the queue by the Prefetcher, to be processed
        self.blocks_event = asyncio.Event()
        self.prefetcher = Prefetcher(
            daemon, env.coin, self.blocks_event,
            polling_delay_secs=env.daemon_poll_interval_blocks_msec/1000,
        )
        self.logger = class_logger(__name__, self.__class__.__name__)

        # Check if GIL is enabled.  note: instantiating the DB can force-enable the GIL
        # if it imports compiled extensions that don't declare free-threading compatibility.
        # By now, DB() init has already run, so no more changes are expected.
        self._gil_enabled = sys._is_gil_enabled() if hasattr(sys, "_is_gil_enabled") else True
        self.logger.info(f'Python GIL enabled: {self._gil_enabled}')

        # Meta
        self.next_cache_check = 0
        self.touched_hashxs = set()     # type: Set[bytes]
        self.touched_outpoints = set()  # type: Set[Tuple[bytes, int]]
        self.reorg_count = 0
        self.height = -1
        self.tip = None  # type: Optional[bytes]  # block_hash_rev
        self.tip_advanced_event = asyncio.Event()
        self.tx_count = 0
        self._caught_up_event = None

        # Caches of unflushed items.
        self.headers = []  # type: list[bytes]
        self._bhash_to_bheight = dict()  # type: dict[bytes, int]
        self.txids_rev = []  # type: List[bytes]
        self.undo_infos = dict()  # type: dict[int, Sequence[bytes]]

        # UTXO cache
        self.utxo_cache = {}
        self.db_deletes = []

        # If the lock is successfully acquired, in-memory chain state
        # is consistent with self.height
        self.state_lock = asyncio.Lock()

        # Signalled after backing up during a reorg
        self.backed_up_event = asyncio.Event()

    async def run_in_thread_with_lock(self, func, *args):
        # Run in a thread to prevent blocking.  Shielded so that
        # cancellations from shutdown don't lose work - when the task
        # completes the data will be flushed and then we shut down.
        # Take the state lock to be certain in-memory state is
        # consistent and not being updated elsewhere.
        async def run_in_thread_locked():
            async with self.state_lock:
                return await run_in_thread(func, *args)
        return await asyncio.shield(run_in_thread_locked())

    async def check_and_advance_blocks(self, raw_blocks: Sequence[bytes]) -> None:
        '''Process the list of raw blocks passed.  Detects and handles
        reorgs.
        '''
        if not raw_blocks:
            return
        first = self.height + 1
        if self._gil_enabled:
            blocks = [self.coin.block(raw_block, first + n)
                      for n, raw_block in enumerate(raw_blocks)]
        else:
            blocks = self.pool_executor1.map(self.coin.block, raw_blocks, range(first, first + (len(raw_blocks))))
            blocks = list(blocks)  # join threads
        headers = [block.header for block in blocks]
        hprevs = [self.coin.header_prevhash_rev(h) for h in headers]
        chain = [self.tip] + [self.coin.header_hash_rev(h) for h in headers[:-1]]

        if hprevs == chain:
            start = time.monotonic()
            await self.run_in_thread_with_lock(self.advance_blocks, blocks)
            await self._maybe_flush()
            if not self.db.first_sync:
                s = '' if len(blocks) == 1 else 's'
                blocks_size = sum(len(block) for block in raw_blocks) / 1_000_000
                self.logger.info(f'processed {len(blocks):,d} block{s} size {blocks_size:.2f} MB '
                                 f'in {time.monotonic() - start:.1f}s')
            if self._caught_up_event.is_set():
                await self.notifications.on_block(
                    touched_hashxs=self.touched_hashxs,
                    touched_outpoints=self.touched_outpoints,
                    height=self.height,
                )
            self.touched_hashxs = set()
            self.touched_outpoints = set()
        elif hprevs[0] != chain[0]:
            await self.reorg_chain()
        else:
            # It is probably possible but extremely rare that what
            # bitcoind returns doesn't form a chain because it
            # reorg-ed the chain as it was processing the batched
            # block hash requests.  Should this happen it's simplest
            # just to reset the prefetcher and try again.
            self.logger.warning('daemon blocks do not form a chain; '
                                'resetting the prefetcher')
            await self.prefetcher.reset_height(self.height)

    async def reorg_chain(self, count: Optional[int] = None) -> None:
        '''Handle a chain reorganisation.

        Count is the number of blocks to simulate a reorg, or None for
        a real reorg.'''
        if count is None:
            self.logger.info('chain reorg detected')
        else:
            self.logger.info(f'faking a reorg of {count:,d} blocks')
        await self.flush(True)

        async def get_raw_blocks(last_height: int, bhashes_hum: Sequence[str]) -> Sequence[bytes]:
            heights = range(last_height, last_height - len(bhashes_hum), -1)
            try:
                blocks = [self.db.read_raw_block(height) for height in heights]
                self.logger.info(f'read {len(blocks)} blocks from disk')
                return blocks
            except FileNotFoundError:
                return await self.daemon.raw_blocks(bhashes_hum)

        def flush_backup():
            # self.touched_hashxs can include other addresses which is
            # harmless, but remove None.
            self.touched_hashxs.discard(None)
            self.db.flush_backup(self.flush_data(), self.touched_hashxs)

        _start, last, bhashes_rev = await self.reorg_hashes(count)
        # Reverse and convert to hex strings.
        bhashes_hum = [hash_to_hex_str(bhash_rev) for bhash_rev in reversed(bhashes_rev)]
        for bhash_hum in chunks(bhashes_hum, 50):
            raw_blocks = await get_raw_blocks(last, bhash_hum)
            await self.run_in_thread_with_lock(self.backup_blocks, raw_blocks)
            await self.run_in_thread_with_lock(flush_backup)
            last -= len(raw_blocks)
        await self.prefetcher.reset_height(self.height)
        self.backed_up_event.set()
        self.backed_up_event.clear()

    async def reorg_hashes(self, count: Optional[int]) -> tuple[int, int, Sequence[bytes]]:
        '''Return a pair (start, last, hashes) of blocks to back up during a
        reorg.

        The hashes are returned in order of increasing height.  Start
        is the height of the first hash, last of the last.
        '''
        start, count = await self.calc_reorg_range(count)
        last = start + count - 1
        s = '' if count == 1 else 's'
        self.logger.info(f'chain was reorganised replacing {count:,d} '
                         f'block{s} at heights {start:,d}-{last:,d}')

        return start, last, await self.db.fs_block_hashes_rev(start, count)

    async def calc_reorg_range(self, count: Optional[int]) -> tuple[int, int]:
        '''Calculate the reorg range'''

        def diff_pos(hashes1, hashes2):
            '''Returns the index of the first difference in the hash lists.
            If both lists match returns their length.'''
            for n, (hash1, hash2) in enumerate(zip(hashes1, hashes2)):
                if hash1 != hash2:
                    return n
            return len(bhashes_rev)

        if count is None:
            # A real reorg
            start = self.height - 1
            count = 1
            while start > 0:
                bhashes_rev = await self.db.fs_block_hashes_rev(start, count)
                bhashes_hum = [hash_to_hex_str(bhash) for bhash in bhashes_rev]
                d_bhashes_hum = await self.daemon.block_hex_hashes(start, count)
                n = diff_pos(bhashes_hum, d_bhashes_hum)
                if n > 0:
                    start += n
                    break
                count = min(count * 2, start)
                start -= count

            count = (self.height - start) + 1
        else:
            start = (self.height - count) + 1

        return start, count

    def estimate_txs_remaining(self) -> float:
        # Try to estimate how many txs there are to go
        daemon_height = self.daemon.cached_height()
        coin = self.coin
        tail_count = daemon_height - max(self.height, coin.TX_COUNT_HEIGHT)
        # Damp the initial enthusiasm
        realism = max(2.0 - 0.9 * self.height / coin.TX_COUNT_HEIGHT, 1.0)
        return (tail_count * coin.TX_PER_BLOCK +
                max(coin.TX_COUNT - self.tx_count, 0)) * realism

    # - Flushing
    def flush_data(self) -> FlushData:
        '''The data for a flush.  The lock must be taken.'''
        assert self.state_lock.locked()
        return FlushData(
            height=self.height,
            tx_count=self.tx_count,
            headers=self.headers,
            bhash_to_bheight=self._bhash_to_bheight,
            block_txids_rev=self.txids_rev,
            undo_infos=self.undo_infos,
            adds=self.utxo_cache,
            deletes=self.db_deletes,
            tip=self.tip,
        )

    async def flush(self, flush_utxos: bool) -> None:
        # side-effect: fields of FlushData will be cleared (passed by reference)
        def flush():
            self.db.flush_dbs(
                flush_data=self.flush_data(),
                flush_utxos=flush_utxos,
                estimate_txs_remaining=self.estimate_txs_remaining,
            )
        await self.run_in_thread_with_lock(flush)

    async def _maybe_flush(self) -> None:
        # If caught up, flush everything as client queries are
        # performed on the DB.
        if self._caught_up_event.is_set():
            await self.flush(True)
        elif time.monotonic() > self.next_cache_check:
            flush_arg = self.check_cache_size()
            if flush_arg is not None:
                await self.flush(flush_arg)
            self.next_cache_check = time.monotonic() + 30

    def check_cache_size(self) -> Optional[bool]:
        '''Flush a cache if it gets too big.'''
        # Good average estimates based on traversal of subobjects and
        # requesting size from Python (see deep_getsizeof).
        one_MB = 1000*1000
        utxo_cache_size = len(self.utxo_cache) * 205
        db_deletes_size = len(self.db_deletes) * 57
        hist_cache_size = self.db.history.unflushed_memsize()
        # Roughly ntxs * 32 + nblocks * 42
        txids_size = (
                (self.tx_count - self.db.fs_tx_count) * 32
                + (self.height - self.db.fs_height) * 42)
        utxo_MB = (db_deletes_size + utxo_cache_size) // one_MB
        hist_MB = (hist_cache_size + txids_size) // one_MB

        self.logger.info(f'our height: {self.height:,d} daemon: '
                         f'{self.daemon.cached_height():,d} '
                         f'UTXOs {utxo_MB:,d}MB hist {hist_MB:,d}MB')

        # Flush history if it takes up over 20% of cache memory.
        # Flush UTXOs once they take up 80% of cache memory.
        cache_MB = self.env.cache_MB
        if utxo_MB + hist_MB >= cache_MB or hist_MB >= cache_MB // 5:
            return utxo_MB >= cache_MB * 4 // 5
        return None

    def advance_blocks(self, blocks: Sequence['Block']) -> None:
        '''Synchronously advance the blocks.

        It is already verified they correctly connect onto our tip.
        '''
        assert self.state_lock.locked()
        assert blocks
        min_height = self.db.min_undo_height(self.daemon.cached_height())
        coin = self.coin

        tx_num = self.tx_count
        height = self.height
        for block in blocks:
            height += 1
            header_hash = coin.header_hash_rev(block.header)
            self._bhash_to_bheight[header_hash] = height
            self.txids_rev.append(b''.join(tx.txid_rev for tx in block.transactions))
            tx_num += len(block.transactions)
            self.db.tx_counts.append(tx_num)
        self.tx_count = tx_num
        self.db.history.update_tx_count_next(tx_num)

        # process tx outputs
        blk_process_outputs = []  # type: list[Callable[[], None]]
        height = self.height
        for block in blocks:
            height += 1
            func = partial(
                self.advance_txs_process_outputs,
                block.transactions,
                height=height,
            )
            if self._gil_enabled:
                blk_process_outputs.append(func)
            else:
                fut = self.pool_executor1.submit(func)
                blk_process_outputs.append(lambda fut=fut: fut.result())
        for func in blk_process_outputs:
            func()

        # process tx inputs
        blk_process_inputs = []  # type: list[Callable[[], None]]
        height = self.height
        for block in blocks:
            height += 1
            func = partial(
                self.advance_txs_process_inputs,
                block.transactions,
                height=height,
                add_undo_info=height >= min_height,
            )
            if self._gil_enabled:
                blk_process_inputs.append(func)
            else:
                fut = self.pool_executor1.submit(func)
                blk_process_inputs.append(lambda fut=fut: fut.result())
        for func in blk_process_inputs:
            func()

        height = self.height
        for block in blocks:
            height += 1
            if height >= min_height:
                self.db.write_raw_block(block.raw, height)

        headers = [block.header for block in blocks]
        self.height = height
        self.headers += headers
        self.tip = self.coin.header_hash_rev(headers[-1])
        self.tip_advanced_event.set()
        self.tip_advanced_event.clear()
        self.logger.debug(f"new height: {self.height}")

    def advance_txs_process_outputs(
            self,
            txs: Sequence[Tx],
            *,
            height: int,
    ) -> None:

        tx_num_start = self.db.tx_counts[height - 1] if height > 0 else 0
        is_unspendable = (
            is_unspendable_genesis if height >= self.coin.GENESIS_ACTIVATION
            else is_unspendable_legacy)

        # Use local vars for speed in the loops
        script_hashX = self.coin.hashX_from_script
        put_utxo = self.utxo_cache.__setitem__
        update_touched_hashxs = self.touched_hashxs.update
        add_touched_outpoint = self.touched_outpoints.add
        hashXs_by_tx = [[] for _ in txs]  # type: list[list[bytes]]
        _pack_txoutidx = pack_txoutidx
        _pack_sats = pack_satoshis_val
        _pack_txnum = pack_txnum

        # 1. process tx outputs: fund UTXOs
        def process_txouts_for_single_tx(tx_pos: int, tx: Tx) -> None:
            txid_rev = tx.txid_rev
            add_hashXs = hashXs_by_tx[tx_pos].append
            tx_num = tx_num_start + tx_pos
            tx_numb = _pack_txnum(tx_num)

            for idx, txout in enumerate(tx.outputs):
                # Ignore unspendable outputs
                if is_unspendable(txout.pk_script):
                    continue

                # Get the hashX
                hashX = script_hashX(txout.pk_script)
                add_hashXs(hashX)
                put_utxo(txid_rev + _pack_txoutidx(idx),
                         hashX + tx_numb + _pack_sats(txout.value))
                add_touched_outpoint((txid_rev, idx))

        def process_txouts_for_chunk(txs_chunk):
            for tx_pos, tx in txs_chunk:
                process_txouts_for_single_tx(tx_pos=tx_pos, tx=tx)

        if self._gil_enabled:
            process_txouts_for_chunk(enumerate(txs))
        else:
            list(self.pool_executor2.map(
                process_txouts_for_chunk,
                chunks(list(enumerate(txs)), 200),
            ))

        # -- barrier. all threads have been joined.
        for hashXs in hashXs_by_tx:
            update_touched_hashxs(hashXs)

        self.db.history.add_unflushed(hashXs_by_tx, tx_num_start)

    def advance_txs_process_inputs(
            self,
            txs: Sequence[Tx],
            *,
            height: int,
            add_undo_info: bool,
    ) -> None:

        tx_num_start = self.db.tx_counts[height - 1] if height > 0 else 0

        # Use local vars for speed in the loops
        bl_undo_info = [b"" for _ in txs]  # type: list[bytes]
        spend_utxo = self.spend_utxo
        update_touched_hashxs = self.touched_hashxs.update
        add_touched_outpoint = self.touched_outpoints.add
        hashXs_by_tx = [[] for _ in txs]  # type: list[list[bytes]]
        _pack_txoutidx = pack_txoutidx
        _pack_sats = pack_satoshis_val
        _pack_txnum = pack_txnum

        # 2. process tx inputs: spend UTXOs
        # note: we don't care about tx ordering in the block
        def process_txins_for_single_tx(tx_pos: int, tx: Tx) -> None:
            add_hashXs = hashXs_by_tx[tx_pos].append
            tx_undo_info = []  # type: list[bytes]
            tx_undo_info_append = tx_undo_info.append
            for txin in tx.inputs:
                if txin.is_generation():
                    continue
                cache_value = spend_utxo(txin.prev_txid_rev, txin.prev_idx)
                tx_undo_info_append(cache_value)
                add_hashXs(cache_value[:HASHX_LEN])
                prevout_tuple = (txin.prev_txid_rev, txin.prev_idx)
                add_touched_outpoint(prevout_tuple)
            bl_undo_info[tx_pos] = b"".join(tx_undo_info)

        def process_txins_for_chunk(txs_chunk):
            for tx_pos, tx in txs_chunk:
                process_txins_for_single_tx(tx_pos=tx_pos, tx=tx)

        # we split this workload across threads regardless of self._gil_enabled,
        # as spend_utxo() is disk-IO-bound:
        list(self.pool_executor2.map(
            process_txins_for_chunk,
            chunks(list(enumerate(txs)), 200),
        ))

        # -- barrier. all threads have been joined.
        for hashXs in hashXs_by_tx:
            update_touched_hashxs(hashXs)

        if add_undo_info:
            self.undo_infos[height] = bl_undo_info

        self.db.history.add_unflushed(hashXs_by_tx, tx_num_start)

    def backup_blocks(self, raw_blocks: Sequence[bytes]) -> None:
        '''Backup the raw blocks and flush.

        The blocks should be in order of decreasing height, starting at.
        self.height.  A flush is performed once the blocks are backed up.
        '''
        self.db.assert_flushed(self.flush_data())
        assert self.height >= len(raw_blocks)
        genesis_activation = self.coin.GENESIS_ACTIVATION

        coin = self.coin
        for raw_block in raw_blocks:
            # Check and update self.tip
            block = coin.block(raw_block, self.height)
            header_hash = coin.header_hash_rev(block.header)
            if header_hash != self.tip:
                raise ChainError(
                    f'backup block {hash_to_hex_str(header_hash)} not tip '
                    f'{hash_to_hex_str(self.tip)} at height {self.height:,d}'
                )
            self.tip = coin.header_prevhash_rev(block.header)
            is_unspendable = (is_unspendable_genesis if self.height >= genesis_activation
                              else is_unspendable_legacy)
            self.backup_txs(block.transactions, is_unspendable)
            self._bhash_to_bheight[header_hash] = self.height
            self.height -= 1
            self.db.tx_counts.pop()

        self.logger.info(f'backed up to height {self.height:,d}')

    def backup_txs(
            self,
            txs: Sequence[Tx],
            is_unspendable: Callable[[bytes], bool],
    ) -> None:
        # Prevout values, in order down the block (coinbase first if present)
        # undo_info is in reverse block order
        undo_info = self.db.read_undo_info(self.height)
        if undo_info is None:
            raise ChainError(f'no undo information found for height '
                             f'{self.height:,d}')
        n = len(undo_info)

        # Use local vars for speed in the loops
        put_utxo = self.utxo_cache.__setitem__
        spend_utxo = self.spend_utxo
        add_touched_hashx = self.touched_hashxs.add
        add_touched_outpoint = self.touched_outpoints.add
        undo_entry_len = HASHX_LEN + TXNUM_LEN + 8

        for tx in reversed(txs):
            txid_rev = tx.txid_rev
            for idx, txout in enumerate(tx.outputs):
                # Spend the TX outputs.  Be careful with unspendable
                # outputs - we didn't save those in the first place.
                if is_unspendable(txout.pk_script):
                    continue

                # Get the hashX
                cache_value = spend_utxo(txid_rev, idx)
                hashX = cache_value[:HASHX_LEN]
                add_touched_hashx(hashX)
                add_touched_outpoint((txid_rev, idx))

            # Restore the inputs
            for txin in reversed(tx.inputs):
                if txin.is_generation():
                    continue
                n -= undo_entry_len
                undo_item = undo_info[n:n + undo_entry_len]
                prevout = txin.prev_txid_rev + pack_txoutidx(txin.prev_idx)
                put_utxo(prevout, undo_item)
                hashX = undo_item[:HASHX_LEN]
                add_touched_hashx(hashX)
                add_touched_outpoint((txin.prev_txid_rev, txin.prev_idx))

        assert n == 0
        self.tx_count -= len(txs)

    '''An in-memory UTXO cache, representing all changes to UTXO state
    since the last DB flush.

    We want to store millions of these in memory for optimal
    performance during initial sync, because then it is possible to
    spend UTXOs without ever going to the database (other than as an
    entry in the address history, and there is only one such entry per
    TX not per UTXO).  So store them in a Python dictionary with
    binary keys and values.

      Key:    TX_HASH + TX_IDX           (32 + 4 = 36 bytes)
      Value:  HASHX + TX_NUM + VALUE     (11 + 5 + 8 = 24 bytes)

    That's 60 bytes of raw data in-memory.  Python dictionary overhead
    means each entry actually uses about 205 bytes of memory.  So
    almost 5 million UTXOs can fit in 1GB of RAM.  There are
    approximately 42 million UTXOs on bitcoin mainnet at height
    433,000.

    Semantics:

      add:   Add it to the cache dictionary.

      spend: Remove it if in the cache dictionary.  Otherwise it's
             been flushed to the DB.  Each UTXO is responsible for two
             entries in the DB.  Mark them for deletion in the next
             cache flush.

    The UTXO database format has to be able to do two things efficiently:

      1.  Given an address be able to list its UTXOs and their values
          so its balance can be efficiently computed.

      2.  When processing transactions, for each prevout spent - a (txid_rev,
          idx) pair - we have to be able to remove it from the DB.  To send
          notifications to clients we also need to know any address it paid
          to.

    To this end we maintain two "tables", one for each point above:

      1.  Key: b'u' + address_hashX + tx_idx + tx_num
          Value: the UTXO value as a 64-bit unsigned integer

      2.  Key: b'h' + compressed_tx_hash + tx_idx + tx_num
          Value: hashX

    The compressed tx hash is just the first few bytes of the hash of
    the tx in which the UTXO was created.  As this is not unique there
    will be potential collisions so tx_num is also in the key.  When
    looking up a UTXO the prefix space of the compressed hash needs to
    be searched and resolved if necessary with the tx_num.  The
    collision rate is low (<0.1%).
    '''

    def spend_utxo(self, txid_rev: bytes, tx_idx: int) -> bytes:
        '''Spend a UTXO and return (hashX + tx_num + value_sats).

        If the UTXO is not in the cache it must be on disk.  We store
        all UTXOs so not finding one indicates a logic error or DB
        corruption.
        '''
        # Fast track is it being in the cache
        idx_packed = pack_txoutidx(tx_idx)
        cache_value = self.utxo_cache.pop(txid_rev + idx_packed, None)
        if cache_value:
            return cache_value

        # Spend it from the DB.

        # Key: b'h' + compressed_tx_hash + tx_idx + tx_num
        # Value: hashX
        prefix = b'h' + txid_rev[:COMP_TXID_LEN] + idx_packed
        candidates = {db_key: hashX for db_key, hashX
                      in self.db.utxo_db.iterator(prefix=prefix)}

        for hdb_key, hashX in candidates.items():
            tx_num_packed = hdb_key[-TXNUM_LEN:]

            if len(candidates) > 1:
                tx_num = unpack_txnum(tx_num_packed)
                hash, _height = self.db.fs_txid_rev(tx_num)
                if hash != txid_rev:
                    assert hash is not None  # Should always be found
                    continue

            # Key: b'u' + address_hashX + tx_idx + tx_num
            # Value: the UTXO value as a 64-bit unsigned integer
            udb_key = b'u' + hashX + hdb_key[-TXOUTIDX_LEN-TXNUM_LEN:]
            utxo_value_packed = self.db.utxo_db.get(udb_key)
            if utxo_value_packed:
                # Remove both entries for this UTXO
                self.db_deletes.append(hdb_key)
                self.db_deletes.append(udb_key)
                return hashX + tx_num_packed + utxo_value_packed

        raise ChainError(f'UTXO {hash_to_hex_str(txid_rev)} / {tx_idx:,d} not '
                         f'found in "h" table')

    async def _process_prefetched_blocks(self) -> None:
        '''Loop forever processing blocks as they arrive.'''
        while True:
            if self.height == self.daemon.cached_height():
                if not self._caught_up_event.is_set():
                    await self._first_caught_up()
                    self._caught_up_event.set()
            await self.blocks_event.wait()
            self.blocks_event.clear()
            if self.reorg_count:
                await self.reorg_chain(self.reorg_count)
                self.reorg_count = 0
            else:
                blocks = self.prefetcher.get_prefetched_blocks()
                await self.check_and_advance_blocks(blocks)

    async def _first_caught_up(self) -> None:
        self.logger.info(f'caught up to height {self.height}')
        # Flush everything but with first_sync->False state.
        first_sync = self.db.first_sync
        self.db.first_sync = False
        await self.flush(True)
        if first_sync:
            self.logger.info(f'{electrumx.version} synced to '
                             f'height {self.height:,d}')
        # Reopen for serving
        await self.db.open_for_serving()

    async def _first_open_dbs(self) -> None:
        await self.db.open_for_sync()
        self.height = self.db.db_height
        self.tip = self.db.db_tip
        self.tx_count = self.db.db_tx_count

    # --- External API

    async def fetch_and_process_blocks(self, caught_up_event: asyncio.Event) -> None:
        '''Fetch, process and index blocks from the daemon.

        Sets caught_up_event when first caught up.  Flushes to disk
        and shuts down cleanly if cancelled.

        This is mainly because if, during initial sync ElectrumX is
        asked to shut down when a large number of blocks have been
        processed but not written to disk, it should write those to
        disk before exiting, as otherwise a significant amount of work
        could be lost.
        '''
        self._caught_up_event = caught_up_event
        await self._first_open_dbs()
        # create two threadpools:
        # - multi-block level work split happens in pool1,
        # - intra-block (txs) level work split happens in pool2.
        # A single threadpool is not enough as that could deadlock: there might be no more threads
        # to start work, and all existing threads might be waiting on new work they just scheduled.
        # To avoid this, we logically nest the pools: top-level code schedules work onto pool1,
        # code already running in pool1 schedules work onto pool2.
        with ThreadPoolExecutor() as self.pool_executor1:
            with ThreadPoolExecutor() as self.pool_executor2:
                try:
                    async with OldTaskGroup() as group:
                        await group.spawn(self.prefetcher.main_loop(self.height))
                        await group.spawn(self._process_prefetched_blocks())
                # Don't flush for arbitrary exceptions as they might be a cause or consequence of
                # corrupted data
                except CancelledError:
                    self.logger.info('flushing to DB for a clean shutdown...')
                    await self.flush(True)

    def force_chain_reorg(self, count: int) -> bool:
        '''Force a reorg of the given number of blocks.

        Returns True if a reorg is queued, false if not caught up.
        '''
        if self._caught_up_event.is_set():
            self.reorg_count = count
            self.blocks_event.set()
            return True
        return False


class DecredBlockProcessor(BlockProcessor):
    async def calc_reorg_range(self, count):
        start, count = await super().calc_reorg_range(count)
        if start > 0:
            # A reorg in Decred can invalidate the previous block
            start -= 1
            count += 1
        return start, count


class NameIndexBlockProcessor(BlockProcessor):

    def advance_txs(self, txs, is_unspendable):
        result = super().advance_txs(txs, is_unspendable)

        tx_num = self.tx_count - len(txs)
        script_name_hashX = self.coin.name_hashX_from_script
        update_touched_hashxs = self.touched_hashxs.update
        hashXs_by_tx = []
        append_hashXs = hashXs_by_tx.append

        for tx in txs:
            hashXs = []
            append_hashX = hashXs.append

            # Add the new UTXOs and associate them with the name script
            for txout in tx.outputs:
                # Get the hashX of the name script.  Ignore non-name scripts.
                hashX = script_name_hashX(txout.pk_script)
                if hashX:
                    append_hashX(hashX)

            append_hashXs(hashXs)
            update_touched_hashxs(hashXs)
            tx_num += 1

        self.db.history.add_unflushed(hashXs_by_tx, self.tx_count - len(txs))

        return result


class LTORBlockProcessor(BlockProcessor):

    def advance_txs(self, txs, is_unspendable):
        self.txids_rev.append(b''.join(tx.txid_rev for tx in txs))

        # Use local vars for speed in the loops
        undo_info = []
        tx_num = self.tx_count
        script_hashX = self.coin.hashX_from_script
        put_utxo = self.utxo_cache.__setitem__
        spend_utxo = self.spend_utxo
        undo_info_append = undo_info.append
        update_touched_hashxs = self.touched_hashxs.update
        add_touched_outpoint = self.touched_outpoints.add
        _pack_txoutidx = pack_txoutidx
        _pack_sats = pack_satoshis_val
        _pack_txnum = pack_txnum

        hashXs_by_tx = [set() for _ in txs]

        # Add the new UTXOs
        for tx, hashXs in zip(txs, hashXs_by_tx):
            txid_rev = tx.txid_rev
            add_hashXs = hashXs.add
            tx_numb = _pack_txnum(tx_num)

            for idx, txout in enumerate(tx.outputs):
                # Ignore unspendable outputs
                if is_unspendable(txout.pk_script):
                    continue

                # Get the hashX
                hashX = script_hashX(txout.pk_script)
                add_hashXs(hashX)
                put_utxo(txid_rev + _pack_txoutidx(idx),
                         hashX + tx_numb + _pack_sats(txout.value))
                add_touched_outpoint((txid_rev, idx))
            tx_num += 1

        # Spend the inputs
        # A separate for-loop here allows any tx ordering in block.
        for tx, hashXs in zip(txs, hashXs_by_tx):
            add_hashXs = hashXs.add
            for txin in tx.inputs:
                if txin.is_generation():
                    continue
                cache_value = spend_utxo(txin.prev_txid_rev, txin.prev_idx)
                undo_info_append(cache_value)
                add_hashXs(cache_value[:HASHX_LEN])
                prevout_tuple = (txin.prev_txid_rev, txin.prev_idx)
                add_touched_outpoint(prevout_tuple)

        # Update touched set for notifications
        for hashXs in hashXs_by_tx:
            update_touched_hashxs(hashXs)

        self.db.history.add_unflushed(hashXs_by_tx, self.tx_count)

        self.tx_count = tx_num
        self.db.tx_counts.append(tx_num)

        return undo_info

    def backup_txs(self, txs, is_unspendable):
        undo_info = self.db.read_undo_info(self.height)
        if undo_info is None:
            raise ChainError(
                f'no undo information found for height {self.height:,d}'
            )

        # Use local vars for speed in the loops
        put_utxo = self.utxo_cache.__setitem__
        spend_utxo = self.spend_utxo
        add_touched_hashx = self.touched_hashxs.add
        add_touched_outpoint = self.touched_outpoints.add
        undo_entry_len = HASHX_LEN + TXNUM_LEN + 8

        # Restore coins that had been spent
        # (may include coins made then spent in this block)
        n = 0
        for tx in txs:
            for txin in tx.inputs:
                if txin.is_generation():
                    continue
                undo_item = undo_info[n:n + undo_entry_len]
                prevout = txin.prev_txid_rev + pack_txoutidx(txin.prev_idx)
                put_utxo(prevout, undo_item)
                add_touched_hashx(undo_item[:HASHX_LEN])
                add_touched_outpoint((txin.prev_txid_rev, txin.prev_idx))
                n += undo_entry_len

        assert n == len(undo_info)

        # Remove tx outputs made in this block, by spending them.
        for tx in txs:
            txid_rev = tx.txid_rev
            for idx, txout in enumerate(tx.outputs):
                # Spend the TX outputs.  Be careful with unspendable
                # outputs - we didn't save those in the first place.
                if is_unspendable(txout.pk_script):
                    continue

                # Get the hashX
                cache_value = spend_utxo(txid_rev, idx)
                hashX = cache_value[:HASHX_LEN]
                add_touched_hashx(hashX)
                add_touched_outpoint((txid_rev, idx))

        self.tx_count -= len(txs)
