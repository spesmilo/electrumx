# Copyright (c) 2016-2018, Neil Booth
#
# All rights reserved.
#
# See the file "LICENCE" for information about the copyright
# and warranty status of this software.

from asyncio import Event
from typing import Set, Dict, Tuple

from aiorpcx import _version as aiorpcx_version, TaskGroup

import electrumx
from electrumx.lib.server_base import ServerBase
from electrumx.lib.util import version_string
from electrumx.server.db import DB
from electrumx.server.mempool import MemPool, MemPoolAPI
from electrumx.server.session import SessionManager


class Notifications:
    # hashX notifications come from two sources: new blocks and
    # mempool refreshes.
    #
    # A user with a pending transaction is notified after the block it
    # gets in is processed.  Block processing can take an extended
    # time, and the prefetcher might poll the daemon after the mempool
    # code in any case.  In such cases the transaction will not be in
    # the mempool after the mempool refresh.  We want to avoid
    # notifying clients twice - for the mempool refresh and when the
    # block is done.  This object handles that logic by deferring
    # notifications appropriately.

    def __init__(self):
        self._touched_hashxs_mp = {}  # type: Dict[int, Set[bytes]]
        self._touched_hashxs_bp = {}  # type: Dict[int, Set[bytes]]
        self._touched_outpoints_mp = {}  # type: Dict[int, Set[Tuple[bytes, int]]]
        self._touched_outpoints_bp = {}  # type: Dict[int, Set[Tuple[bytes, int]]]
        self._highest_block = -1

    async def _maybe_notify(self):
        th_mp, th_bp = self._touched_hashxs_mp, self._touched_hashxs_bp
        # figure out block height
        common_heights = set(th_mp).intersection(th_bp)
        if common_heights:
            height = max(common_heights)
        elif th_mp and max(th_mp) == self._highest_block:
            height = self._highest_block
        else:
            # Either we are processing a block and waiting for it to
            # come in, or we have not yet had a mempool update for the
            # new block height
            return
        # hashXs
        touched_hashxs = th_mp.pop(height)
        for old in [h for h in th_mp if h <= height]:
            del th_mp[old]
        for old in [h for h in th_bp if h <= height]:
            touched_hashxs.update(th_bp.pop(old))
        # outpoints
        to_mp, to_bp = self._touched_outpoints_mp, self._touched_outpoints_bp
        touched_outpoints = to_mp.pop(height)
        for old in [h for h in to_mp if h <= height]:
            del to_mp[old]
        for old in [h for h in to_bp if h <= height]:
            touched_outpoints.update(to_bp.pop(old))

        await self.notify(
            height=height,
            touched_hashxs=touched_hashxs,
            touched_outpoints=touched_outpoints,
        )

    async def notify(
            self,
            *,
            touched_hashxs: Set[bytes],
            touched_outpoints: Set[Tuple[bytes, int]],
            height: int,
    ):
        pass

    async def start(self, height: int, notify_func):
        self._highest_block = height
        self.notify = notify_func
        await self.notify(
            height=height,
            touched_hashxs=set(),
            touched_outpoints=set(),
        )

    async def on_mempool(
            self,
            *,
            touched_hashxs: Set[bytes],
            touched_outpoints: Set[Tuple[bytes, int]],
            height: int,
    ):
        self._touched_hashxs_mp[height] = touched_hashxs
        self._touched_outpoints_mp[height] = touched_outpoints
        await self._maybe_notify()

    async def on_block(
            self,
            *,
            touched_hashxs: Set[bytes],
            touched_outpoints: Set[Tuple[bytes, int]],
            height: int,
    ):
        self._touched_hashxs_bp[height] = touched_hashxs
        self._touched_outpoints_bp[height] = touched_outpoints
        self._highest_block = height
        await self._maybe_notify()


class Controller(ServerBase):
    '''Manages server initialisation and stutdown.

    Servers are started once the mempool is synced after the block
    processor first catches up with the daemon.
    '''
    async def serve(self, shutdown_event):
        '''Start the RPC server and wait for the mempool to synchronize.  Then
        start serving external clients.
        '''
        if not (0, 18, 5) <= aiorpcx_version < (0, 19):
            raise RuntimeError('aiorpcX version 0.18.5+ is required')

        env = self.env
        min_str, max_str = env.coin.SESSIONCLS.protocol_min_max_strings()
        self.logger.info(f'software version: {electrumx.version}')
        self.logger.info(f'aiorpcX version: {version_string(aiorpcx_version)}')
        self.logger.info(f'supported protocol versions: {min_str}-{max_str}')
        self.logger.info(f'event loop policy: {env.loop_policy}')
        self.logger.info(f'reorg limit is {env.reorg_limit:,d} blocks')

        notifications = Notifications()
        Daemon = env.coin.DAEMON
        BlockProcessor = env.coin.BLOCK_PROCESSOR

        async with Daemon(env.coin, env.daemon_url) as daemon:
            db = DB(env)
            bp = BlockProcessor(env, db, daemon, notifications)

            # Set notifications up to implement the MemPoolAPI
            def get_db_height():
                return db.db_height
            notifications.height = daemon.height
            notifications.db_height = get_db_height
            notifications.cached_height = daemon.cached_height
            notifications.mempool_hashes = daemon.mempool_hashes
            notifications.raw_transactions = daemon.getrawtransactions
            notifications.lookup_utxos = db.lookup_utxos
            MemPoolAPI.register(Notifications)
            mempool = MemPool(env.coin, notifications)

            session_mgr = SessionManager(env, db, bp, daemon, mempool,
                                         shutdown_event)

            # Test daemon authentication, and also ensure it has a cached
            # height.  Do this before entering the task group.
            await daemon.height()

            caught_up_event = Event()
            mempool_event = Event()

            async def wait_for_catchup():
                await caught_up_event.wait()
                await group.spawn(db.populate_header_merkle_cache())
                await group.spawn(mempool.keep_synchronized(mempool_event))

            async with TaskGroup() as group:
                await group.spawn(session_mgr.serve(notifications, mempool_event))
                await group.spawn(bp.fetch_and_process_blocks(caught_up_event))
                await group.spawn(wait_for_catchup())
