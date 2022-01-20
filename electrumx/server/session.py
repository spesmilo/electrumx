# Copyright (c) 2016-2018, Neil Booth
#
# All rights reserved.
#
# See the file "LICENCE" for information about the copyright
# and warranty status of this software.

'''Classes for local RPC server and remote client TCP/SSL servers.'''

import asyncio
import codecs
import datetime
import itertools
import math
import os
import ssl
import time
from collections import defaultdict
from functools import partial
from ipaddress import IPv4Address, IPv6Address, IPv4Network, IPv6Network
from typing import (Optional, TYPE_CHECKING, Tuple, Sequence, Set, Dict, Iterable, Any, Mapping,
                    List)
import asyncio

import attr
import pylru
from aiorpcx import (Event, JSONRPCAutoDetect, JSONRPCConnection,
                     ReplyAndDisconnect, Request, RPCError, RPCSession,
                     TaskGroup, handler_invocation, serve_rs, serve_ws, sleep,
                     NewlineFramer)

import electrumx
import electrumx.lib.util as util
from electrumx.lib.hash import (HASHX_LEN, Base58Error, hash_to_hex_str,
                                hex_str_to_hash, sha256)
from electrumx.lib.merkle import MerkleCache
from electrumx.lib.text import sessions_lines
from electrumx.server.daemon import DaemonError
from electrumx.server.peers import PeerManager

if TYPE_CHECKING:
    from electrumx.server.db import DB
    from electrumx.server.env import Env
    from electrumx.server.block_processor import BlockProcessor
    from electrumx.server.daemon import Daemon
    from electrumx.server.mempool import MemPool


BAD_REQUEST = 1
DAEMON_ERROR = 2


def scripthash_to_hashX(scripthash):
    try:
        bin_hash = hex_str_to_hash(scripthash)
        if len(bin_hash) == 32:
            return bin_hash[:HASHX_LEN]
    except (ValueError, TypeError):
        pass
    raise RPCError(BAD_REQUEST, f'{scripthash} is not a valid script hash')


def non_negative_integer(value):
    '''Return param value it is or can be converted to a non-negative
    integer, otherwise raise an RPCError.'''
    try:
        value = int(value)
        if value >= 0:
            return value
    except (ValueError, TypeError):
        pass
    raise RPCError(BAD_REQUEST,
                   f'{value} should be a non-negative integer')


def integer(value):
    '''Return param value it is or can be converted to an
    integer, otherwise raise an RPCError.'''
    try:
        return int(value)
    except (ValueError, TypeError):
        pass
    raise RPCError(BAD_REQUEST,
                   f'{value} should be a non-negative integer')


def assert_boolean(value):
    '''Return param value it is boolean otherwise raise an RPCError.'''
    if value in (False, True):
        return value
    raise RPCError(BAD_REQUEST, f'{value} should be a boolean value')


def assert_tx_hash(value):
    '''Raise an RPCError if the value is not a valid hexadecimal transaction hash.

    If it is valid, return it as 32-byte binary hash.
    '''
    try:
        raw_hash = hex_str_to_hash(value)
        if len(raw_hash) == 32:
            return raw_hash
    except (ValueError, TypeError):
        pass
    raise RPCError(BAD_REQUEST, f'{value} should be a transaction hash')


def assert_status_hash(value):
    '''Raise an RPCError if the value is not a valid hexadecimal scripthash status.

    If it is valid, return it as 32-byte binary hash.
    '''
    # note that unlike for tx_hash, we keep the endianness here
    try:
        raw_hash = util.hex_to_bytes(value)
        if len(raw_hash) == 32:
            return raw_hash
    except (ValueError, TypeError):
        pass
    raise RPCError(BAD_REQUEST, f'{value} should be a scripthash status')


def is_hex_str(text: Any) -> bool:
    if not isinstance(text, str):
        return False
    try:
        b = bytes.fromhex(text)
    except Exception:
        return False
    # forbid whitespaces in text:
    if len(text) != 2 * len(b):
        return False
    return True


def assert_hex_str(value: Any) -> None:
    if not is_hex_str(value):
        raise RPCError(BAD_REQUEST, f'{value} should be a hex str')


# Constants used to limit the size of returned history to ensure it fits within a response.
# These are all overestimating somewhat, for paranoia.
HISTORY_OVER_WIRE_OVERHEAD_BYTES = 200  # a few bytes are reserved for overhead
HISTORY_CONF_ITEM_SIZE_BYTES = 107      # a conf tx item is ~this many bytes when JSON-encoded
HISTORY_UNCONF_ITEM_SIZE_BYTES = 140    # a mempool tx item is ~this many bytes when JSON-encoded


@attr.s(slots=True)
class SessionGroup:
    name = attr.ib()
    weight = attr.ib()
    sessions = attr.ib()  # type: Set[ElectrumX]
    retained_cost = attr.ib()

    def session_cost(self):
        return sum(session.cost for session in self.sessions)

    def cost(self):
        return self.retained_cost + self.session_cost()


@attr.s(slots=True)
class SessionReferences:
    # All attributes are sets but groups is a list
    sessions = attr.ib()
    groups = attr.ib()
    specials = attr.ib()    # Lower-case strings
    unknown = attr.ib()     # Strings


class SessionManager:
    '''Holds global state about all sessions.'''

    def __init__(
            self,
            env: 'Env',
            db: 'DB',
            bp: 'BlockProcessor',
            daemon: 'Daemon',
            mempool: 'MemPool',
            shutdown_event: asyncio.Event,
    ):
        env.max_send = max(350000, env.max_send)
        self.env = env
        self.db = db
        self.bp = bp
        self.daemon = daemon
        self.mempool = mempool
        self.peer_mgr = PeerManager(env, db)
        self.shutdown_event = shutdown_event
        self.logger = util.class_logger(__name__, self.__class__.__name__)
        self.servers = {}           # service->server
        self.sessions = {}          # type: Dict[ElectrumX, Iterable[SessionGroup]]
        self.session_groups = {}    # type: Dict[str, SessionGroup]
        self.txs_sent = 0
        # Would use monotonic time, but aiorpcx sessions use Unix time:
        self.start_time = time.time()
        self._method_counts = defaultdict(int)
        self._reorg_count = 0
        self._tx_hashes_cache = pylru.lrucache(1000)
        self._tx_hashes_lookups = 0
        self._tx_hashes_hits = 0
        # Really a MerkleCache cache
        self._merkle_cache = pylru.lrucache(1000)
        self._merkle_lookups = 0
        self._merkle_hits = 0
        self.estimatefee_cache = pylru.lrucache(1000)
        self.notified_height = None
        self.hsub_results = None
        self._task_group = TaskGroup()
        self._sslc = None
        # Event triggered when electrumx is listening for incoming requests.
        self.server_listening = Event()
        self.session_event = Event()

        # Set up the RPC request handlers
        cmds = ('add_peer daemon_url disconnect getinfo groups log peers '
                'query reorg sessions stop'.split())
        LocalRPC.request_handlers = {cmd: getattr(self, 'rpc_' + cmd)
                                     for cmd in cmds}

    def _ssl_context(self):
        if self._sslc is None:
            self._sslc = ssl.SSLContext(ssl.PROTOCOL_TLS)
            self._sslc.load_cert_chain(self.env.ssl_certfile, keyfile=self.env.ssl_keyfile)
        return self._sslc

    async def _start_servers(self, services):
        for service in services:
            kind = service.protocol.upper()
            if service.protocol in self.env.SSL_PROTOCOLS:
                sslc = self._ssl_context()
            else:
                sslc = None
            if service.protocol == 'rpc':
                session_class = LocalRPC
            else:
                session_class = self.env.coin.SESSIONCLS
            if service.protocol in ('ws', 'wss'):
                serve = serve_ws
            else:
                serve = serve_rs
            # FIXME: pass the service not the kind
            session_factory = partial(session_class, self, self.db, self.mempool,
                                      self.peer_mgr, kind)
            host = None if service.host == 'all_interfaces' else str(service.host)
            try:
                self.servers[service] = await serve(session_factory, host,
                                                    service.port, ssl=sslc)
            except OSError as e:    # don't suppress CancelledError
                self.logger.error(f'{kind} server failed to listen on {service.address}: {e}')
            else:
                self.logger.info(f'{kind} server listening on {service.address}')

    async def _start_external_servers(self):
        '''Start listening on TCP and SSL ports, but only if the respective
        port was given in the environment.
        '''
        await self._start_servers(service for service in self.env.services
                                  if service.protocol != 'rpc')
        self.server_listening.set()

    async def _stop_servers(self, services):
        '''Stop the servers of the given protocols.'''
        server_map = {service: self.servers.pop(service)
                      for service in set(services).intersection(self.servers)}
        # Close all before waiting
        for service, server in server_map.items():
            self.logger.info(f'closing down server for {service}')
            server.close()
        # No value in doing these concurrently
        for server in server_map.values():
            await server.wait_closed()

    async def _manage_servers(self):
        paused = False
        max_sessions = self.env.max_sessions
        low_watermark = max_sessions * 19 // 20
        while True:
            await self.session_event.wait()
            self.session_event.clear()
            if not paused and len(self.sessions) >= max_sessions:
                self.logger.info(f'maximum sessions {max_sessions:,d} '
                                 f'reached, stopping new connections until '
                                 f'count drops to {low_watermark:,d}')
                await self._stop_servers(service for service in self.servers
                                         if service.protocol != 'rpc')
                paused = True
            # Start listening for incoming connections if paused and
            # session count has fallen
            if paused and len(self.sessions) <= low_watermark:
                self.logger.info('resuming listening for incoming connections')
                await self._start_external_servers()
                paused = False

    async def _log_sessions(self):
        '''Periodically log sessions.'''
        log_interval = self.env.log_sessions
        if log_interval:
            while True:
                await sleep(log_interval)
                data = self._session_data(for_log=True)
                for line in sessions_lines(data):
                    self.logger.info(line)
                self.logger.info(util.json_serialize(self._get_info()))

    async def _disconnect_sessions(self, sessions, reason, *, force_after=1.0):
        if sessions:
            session_ids = ', '.join(str(session.session_id) for session in sessions)
            self.logger.info(f'{reason} session ids {session_ids}')
            for session in sessions:
                await self._task_group.spawn(session.close(force_after=force_after))

    async def _clear_stale_sessions(self):
        '''Cut off sessions that haven't done anything for 10 minutes.'''
        while True:
            await sleep(60)
            stale_cutoff = time.time() - self.env.session_timeout
            stale_sessions = [session for session in self.sessions
                              if session.last_recv < stale_cutoff]
            await self._disconnect_sessions(stale_sessions, 'closing stale')
            del stale_sessions

    async def _handle_chain_reorgs(self):
        '''Clear certain caches on chain reorgs.'''
        while True:
            await self.bp.backed_up_event.wait()
            self.logger.info(f'reorg signalled; clearing tx_hashes and merkle caches')
            self._reorg_count += 1
            self._tx_hashes_cache.clear()
            self._merkle_cache.clear()

    async def _recalc_concurrency(self):
        '''Periodically recalculate session concurrency.'''
        session_class = self.env.coin.SESSIONCLS
        period = 300
        while True:
            await sleep(period)
            hard_limit = session_class.cost_hard_limit

            # Reduce retained group cost
            refund = period * hard_limit / 5000
            dead_groups = []
            for group in self.session_groups.values():
                group.retained_cost = max(0.0, group.retained_cost - refund)
                if group.retained_cost == 0 and not group.sessions:
                    dead_groups.append(group)
            # Remove dead groups
            for group in dead_groups:
                self.session_groups.pop(group.name)

            # Recalc concurrency for sessions where cost is changing gradually, and update
            # cost_decay_per_sec.
            for session in self.sessions:
                # Subs have an on-going cost so decay more slowly with more subs
                session.cost_decay_per_sec = hard_limit / (10000 + 5 * session.sub_count_total())
                session.recalc_concurrency()

    def _get_info(self):
        '''A summary of server state.'''
        cache_fmt = '{:,d} lookups {:,d} hits {:,d} entries'
        sessions = self.sessions
        return {
            'coin': self.env.coin.__name__,
            'daemon': self.daemon.logged_url(),
            'daemon height': self.daemon.cached_height(),
            'db height': self.db.db_height,
            'groups': len(self.session_groups),
            'merkle cache': cache_fmt.format(
                self._merkle_lookups, self._merkle_hits, len(self._merkle_cache)),
            'pid': os.getpid(),
            'peers': self.peer_mgr.info(),
            'request counts': self._method_counts,
            'request total': sum(self._method_counts.values()),
            'sessions': {
                'count': len(sessions),
                'count with subs_sh': sum(s.sub_count_scripthashes() > 0 for s in sessions),
                'count with subs_txo': sum(s.sub_count_txoutpoints() > 0 for s in sessions),
                'count with subs_any': sum(s.sub_count_total() > 0 for s in sessions),
                'errors': sum(s.errors for s in sessions),
                'logged': len([s for s in sessions if s.log_me]),
                'pending requests': sum(s.unanswered_request_count() for s in sessions),
                'subs_sh': sum(s.sub_count_scripthashes() for s in sessions),
                'subs_txo': sum(s.sub_count_txoutpoints() for s in sessions),
            },
            'tx hashes cache': cache_fmt.format(
                self._tx_hashes_lookups, self._tx_hashes_hits, len(self._tx_hashes_cache)),
            'txs sent': self.txs_sent,
            'uptime': util.formatted_time(time.time() - self.start_time),
            'version': electrumx.version,
        }

    def _session_data(self, for_log):
        '''Returned to the RPC 'sessions' call.'''
        now = time.time()
        sessions = sorted(self.sessions, key=lambda s: s.start_time)
        return [(session.session_id,
                 session.flags(),
                 session.remote_address_string(for_log=for_log),
                 session.client,
                 session.protocol_version_string(),
                 session.cost,
                 session.extra_cost(),
                 session.unanswered_request_count(),
                 session.txs_sent,
                 session.sub_count_total(),
                 session.recv_count, session.recv_size,
                 session.send_count, session.send_size,
                 now - session.start_time)
                for session in sessions]

    def _group_data(self):
        '''Returned to the RPC 'groups' call.'''
        result = []
        for name, group in self.session_groups.items():
            sessions = group.sessions
            result.append([name,
                           len(sessions),
                           group.session_cost(),
                           group.retained_cost,
                           sum(s.unanswered_request_count() for s in sessions),
                           sum(s.txs_sent for s in sessions),
                           sum(s.sub_count_total() for s in sessions),
                           sum(s.recv_count for s in sessions),
                           sum(s.recv_size for s in sessions),
                           sum(s.send_count for s in sessions),
                           sum(s.send_size for s in sessions),
                           ])
        return result

    async def _refresh_hsub_results(self, height):
        '''Refresh the cached header subscription responses to be for height,
        and record that as notified_height.
        '''
        # Paranoia: a reorg could race and leave db_height lower
        height = min(height, self.db.db_height)
        raw = await self.raw_header(height)
        self.hsub_results = {'hex': raw.hex(), 'height': height}
        self.notified_height = height

    def _session_references(self, items, special_strings):
        '''Return a SessionReferences object.'''
        if not isinstance(items, list) or not all(isinstance(item, str) for item in items):
            raise RPCError(BAD_REQUEST, 'expected a list of session IDs')

        sessions_by_id = {session.session_id: session for session in self.sessions}
        groups_by_name = self.session_groups

        sessions = set()
        groups = set()     # Names as groups are not hashable
        specials = set()
        unknown = set()

        for item in items:
            if item.isdigit():
                session = sessions_by_id.get(int(item))
                if session:
                    sessions.add(session)
                else:
                    unknown.add(item)
            else:
                lc_item = item.lower()
                if lc_item in special_strings:
                    specials.add(lc_item)
                else:
                    if lc_item in groups_by_name:
                        groups.add(lc_item)
                    else:
                        unknown.add(item)

        groups = [groups_by_name[group] for group in groups]
        return SessionReferences(sessions, groups, specials, unknown)

    # --- LocalRPC command handlers

    async def rpc_add_peer(self, real_name):
        '''Add a peer.

        real_name: "bch.electrumx.cash t50001 s50002" for example
        '''
        await self.peer_mgr.add_localRPC_peer(real_name)
        return f"peer '{real_name}' added"

    async def rpc_disconnect(self, session_ids):
        '''Disconnect sesssions.

        session_ids: array of session IDs
        '''
        refs = self._session_references(session_ids, {'all'})
        result = []

        if 'all' in refs.specials:
            sessions = self.sessions
            result.append('disconnecting all sessions')
        else:
            sessions = refs.sessions
            result.extend(f'disconnecting session {session.session_id}' for session in sessions)
            for group in refs.groups:
                result.append(f'disconnecting group {group.name}')
                sessions.update(group.sessions)
        result.extend(f'unknown: {item}' for item in refs.unknown)

        await self._disconnect_sessions(sessions, 'local RPC request to disconnect')
        return result

    async def rpc_log(self, session_ids):
        '''Toggle logging of sesssions.

        session_ids: array of session or group IDs, or 'all', 'none', 'new'
        '''
        refs = self._session_references(session_ids, {'all', 'none', 'new'})
        result = []

        def add_result(text, value):
            result.append(f'logging {text}' if value else f'not logging {text}')

        if 'all' in refs.specials:
            for session in self.sessions:
                session.log_me = True
            SessionBase.log_new = True
            result.append('logging all sessions')
        if 'none' in refs.specials:
            for session in self.sessions:
                session.log_me = False
            SessionBase.log_new = False
            result.append('logging no sessions')
        if 'new' in refs.specials:
            SessionBase.log_new = not SessionBase.log_new
            add_result('new sessions', SessionBase.log_new)

        sessions = refs.sessions
        for session in sessions:
            session.log_me = not session.log_me
            add_result(f'session {session.session_id}', session.log_me)
        for group in refs.groups:
            for session in group.sessions.difference(sessions):
                sessions.add(session)
                session.log_me = not session.log_me
                add_result(f'session {session.session_id}', session.log_me)

        result.extend(f'unknown: {item}' for item in refs.unknown)
        return result

    async def rpc_daemon_url(self, daemon_url):
        '''Replace the daemon URL.'''
        daemon_url = daemon_url or self.env.daemon_url
        try:
            self.daemon.set_url(daemon_url)
        except Exception as e:
            raise RPCError(BAD_REQUEST, f'an error occured: {e!r}')
        return f'now using daemon at {self.daemon.logged_url()}'

    async def rpc_stop(self):
        '''Shut down the server cleanly.'''
        self.shutdown_event.set()
        return 'stopping'

    async def rpc_getinfo(self):
        '''Return summary information about the server process.'''
        return self._get_info()

    async def rpc_groups(self):
        '''Return statistics about the session groups.'''
        return self._group_data()

    async def rpc_peers(self):
        '''Return a list of data about server peers.'''
        return self.peer_mgr.rpc_data()

    async def rpc_query(self, items, limit):
        '''Returns data about a script, address or name.'''
        coin = self.env.coin
        db = self.db
        lines = []

        def arg_to_hashX(arg):
            try:
                script = bytes.fromhex(arg)
                lines.append(f'Script: {arg}')
                return coin.hashX_from_script(script)
            except ValueError:
                pass

            try:
                hashX = coin.address_to_hashX(arg)
                lines.append(f'Address: {arg}')
                return hashX
            except Base58Error:
                pass

            try:
                script = coin.build_name_index_script(arg.encode("ascii"))
                hashX = coin.name_hashX_from_script(script)
                lines.append(f'Name: {arg}')
                return hashX
            except (AttributeError, UnicodeEncodeError):
                pass

            return None

        for arg in items:
            hashX = arg_to_hashX(arg)
            if not hashX:
                continue
            n = None
            history = await db.limited_history(hashX=hashX, limit=limit)
            for n, (tx_hash, height) in enumerate(history):
                lines.append(f'History #{n:,d}: height {height:,d} '
                             f'tx_hash {hash_to_hex_str(tx_hash)}')
            if n is None:
                lines.append('No history found')
            n = None
            utxos = await db.all_utxos(hashX)
            for n, utxo in enumerate(utxos, start=1):
                lines.append(f'UTXO #{n:,d}: tx_hash '
                             f'{hash_to_hex_str(utxo.tx_hash)} '
                             f'tx_pos {utxo.tx_pos:,d} height '
                             f'{utxo.height:,d} value {utxo.value:,d}')
                if n == limit:
                    break
            if n is None:
                lines.append('No UTXOs found')

            balance = sum(utxo.value for utxo in utxos)
            lines.append(f'Balance: {coin.decimal_value(balance):,f} '
                         f'{coin.SHORTNAME}')

        return lines

    async def rpc_sessions(self):
        '''Return statistics about connected sessions.'''
        return self._session_data(for_log=False)

    async def rpc_reorg(self, count):
        '''Force a reorg of the given number of blocks.

        count: number of blocks to reorg
        '''
        count = non_negative_integer(count)
        if not self.bp.force_chain_reorg(count):
            raise RPCError(BAD_REQUEST, 'still catching up with daemon')
        return f'scheduled a reorg of {count:,d} blocks'

    # --- External Interface

    async def serve(self, notifications, event):
        '''Start the RPC server if enabled.  When the event is triggered,
        start TCP and SSL servers.'''
        try:
            await self._start_servers(service for service in self.env.services
                                      if service.protocol == 'rpc')
            await event.wait()

            session_class = self.env.coin.SESSIONCLS
            session_class.cost_soft_limit = self.env.cost_soft_limit
            session_class.cost_hard_limit = self.env.cost_hard_limit
            session_class.cost_decay_per_sec = session_class.cost_hard_limit / 10000
            session_class.bw_cost_per_byte = 1.0 / self.env.bw_unit_cost
            session_class.cost_sleep = self.env.request_sleep / 1000
            session_class.initial_concurrent = self.env.initial_concurrent
            session_class.processing_timeout = self.env.request_timeout

            self.logger.info(f'max session count: {self.env.max_sessions:,d}')
            self.logger.info(f'session timeout: {self.env.session_timeout:,d} seconds')
            self.logger.info(f'session cost hard limit {self.env.cost_hard_limit:,d}')
            self.logger.info(f'session cost soft limit {self.env.cost_soft_limit:,d}')
            self.logger.info(f'bandwidth unit cost {self.env.bw_unit_cost:,d}')
            self.logger.info(f'request sleep {self.env.request_sleep:,d}ms')
            self.logger.info(f'request timeout {self.env.request_timeout:,d}s')
            self.logger.info(f'initial concurrent {self.env.initial_concurrent:,d}')

            self.logger.info(f'max response size {self.env.max_send:,d} bytes')
            if self.env.drop_client is not None:
                self.logger.info(
                    f'drop clients matching: {self.env.drop_client.pattern}'
                )
            for service in self.env.report_services:
                self.logger.info(f'advertising service {service}')
            # Start notifications; initialize hsub_results
            await notifications.start(self.db.db_height, self._notify_sessions)
            await self._start_external_servers()
            # Peer discovery should start after the external servers
            # because we connect to ourself
            async with self._task_group as group:
                await group.spawn(self.peer_mgr.discover_peers())
                await group.spawn(self._clear_stale_sessions())
                await group.spawn(self._handle_chain_reorgs())
                await group.spawn(self._recalc_concurrency())
                await group.spawn(self._log_sessions())
                await group.spawn(self._manage_servers())
        finally:
            # Close servers then sessions
            await self._stop_servers(self.servers.keys())
            async with TaskGroup() as group:
                for session in list(self.sessions):
                    await group.spawn(session.close(force_after=1))

    def extra_cost(self, session):
        # Note there is no guarantee that session is still in self.sessions.  Example traceback:
        # notify_sessions->notify->address_status->bump_cost->recalc_concurrency->extra_cost
        # during which there are many places the sesssion could be removed
        groups = self.sessions.get(session)
        if groups is None:
            return 0
        return sum((group.cost() - session.cost) * group.weight for group in groups)

    async def _merkle_branch(self, height, tx_hashes, tx_pos):
        tx_hash_count = len(tx_hashes)
        cost = tx_hash_count

        if tx_hash_count >= 200:
            self._merkle_lookups += 1
            merkle_cache = self._merkle_cache.get(height)
            if merkle_cache:
                self._merkle_hits += 1
                cost = 10 * math.sqrt(tx_hash_count)
            else:
                async def tx_hashes_func(start, count):
                    return tx_hashes[start: start + count]

                merkle_cache = MerkleCache(self.db.merkle, tx_hashes_func)
                self._merkle_cache[height] = merkle_cache
                await merkle_cache.initialize(len(tx_hashes))
            branch, _root = await merkle_cache.branch_and_root(tx_hash_count, tx_pos)
        else:
            branch, _root = self.db.merkle.branch_and_root(tx_hashes, tx_pos)

        branch = [hash_to_hex_str(hash) for hash in branch]
        return branch, cost / 2500

    async def merkle_branch_for_tx_hash(
            self, *, tx_hash: bytes, height: int = None,
    ) -> Tuple[int, Sequence[str], int, float]:
        '''Returns (height, branch, tx_pos, cost).'''
        cost = 0
        tx_pos = None
        if height is None:
            cost += 0.1
            height, tx_pos = await self.db.get_blockheight_and_txpos_for_txhash(tx_hash)
        if height is None:
            raise RPCError(BAD_REQUEST,
                           f'tx {hash_to_hex_str(tx_hash)} not in any block')
        tx_hashes, tx_hashes_cost = await self.tx_hashes_at_blockheight(height)
        if tx_pos is None:
            try:
                tx_pos = tx_hashes.index(tx_hash)
            except ValueError:
                raise RPCError(BAD_REQUEST,
                               f'tx {hash_to_hex_str(tx_hash)} not in block at height {height:,d}')
        elif not (len(tx_hashes) > tx_pos and tx_hashes[tx_pos] == tx_hash):
            # there was a reorg while processing the request... TODO maybe retry?
            raise RPCError(BAD_REQUEST,
                           f'tx {hash_to_hex_str(tx_hash)} was reorged while processing request')
        branch, merkle_cost = await self._merkle_branch(height, tx_hashes, tx_pos)
        cost += tx_hashes_cost + merkle_cost
        return height, branch, tx_pos, cost

    async def merkle_branch_for_tx_pos(self, height, tx_pos):
        '''Return a triple (branch, tx_hash_hex, cost).'''
        tx_hashes, tx_hashes_cost = await self.tx_hashes_at_blockheight(height)
        try:
            tx_hash = tx_hashes[tx_pos]
        except IndexError:
            raise RPCError(BAD_REQUEST,
                           f'no tx at position {tx_pos:,d} in block at height {height:,d}')
        branch, merkle_cost = await self._merkle_branch(height, tx_hashes, tx_pos)
        return branch, hash_to_hex_str(tx_hash), tx_hashes_cost + merkle_cost

    async def tx_hashes_at_blockheight(self, height):
        '''Returns a pair (tx_hashes, cost).

        tx_hashes is an ordered list of binary hashes, cost is an estimated cost of
        getting the hashes; cheaper if in-cache.  Raises RPCError.
        '''
        self._tx_hashes_lookups += 1
        tx_hashes = self._tx_hashes_cache.get(height)
        if tx_hashes:
            self._tx_hashes_hits += 1
            return tx_hashes, 0.1

        # Ensure the tx_hashes are fresh before placing in the cache
        while True:
            reorg_count = self._reorg_count
            try:
                tx_hashes = await self.db.tx_hashes_at_blockheight(height)
            except self.db.DBError as e:
                raise RPCError(BAD_REQUEST, f'db error: {e!r}')
            if reorg_count == self._reorg_count:
                break

        self._tx_hashes_cache[height] = tx_hashes

        return tx_hashes, 0.25 + len(tx_hashes) * 0.0001

    def session_count(self):
        '''The number of connections that we've sent something to.'''
        return len(self.sessions)

    async def daemon_request(self, method, *args):
        '''Catch a DaemonError and convert it to an RPCError.'''
        try:
            return await getattr(self.daemon, method)(*args)
        except DaemonError as e:
            raise RPCError(DAEMON_ERROR, f'daemon error: {e!r}') from None

    async def raw_header(self, height):
        '''Return the binary header at the given height.'''
        try:
            return await self.db.raw_header(height)
        except IndexError:
            raise RPCError(BAD_REQUEST, f'height {height:,d} '
                           'out of range') from None

    async def broadcast_transaction(self, raw_tx):
        hex_hash = await self.daemon.broadcast_transaction(raw_tx)
        self.txs_sent += 1
        return hex_hash

    async def _notify_sessions(
            self,
            *,
            touched_hashxs: Set[bytes],
            touched_outpoints: Set[Tuple[bytes, int]],
            height: int,
    ):
        '''Notify sessions about height changes and touched addresses.'''
        height_changed = height != self.notified_height
        if height_changed:
            await self._refresh_hsub_results(height)

        for session in self.sessions:
            coro = session.notify(
                touched_hashxs=touched_hashxs,
                touched_outpoints=touched_outpoints,
                height_changed=height_changed,
            )
            await self._task_group.spawn(coro)

    def _ip_addr_group_name(self, session) -> Optional[str]:
        host = session.remote_address().host
        if isinstance(host, (IPv4Address, IPv6Address)):
            if host.is_private:  # exempt private addresses
                return None
            if isinstance(host, IPv4Address):
                subnet_size = self.env.session_group_by_subnet_ipv4
                subnet = IPv4Network(host).supernet(prefixlen_diff=32 - subnet_size)
                return str(subnet)
            elif isinstance(host, IPv6Address):
                subnet_size = self.env.session_group_by_subnet_ipv6
                subnet = IPv6Network(host).supernet(prefixlen_diff=128 - subnet_size)
                return str(subnet)
        return 'unknown_addr'

    def _session_group(self, name: Optional[str], weight: float) -> Optional[SessionGroup]:
        if name is None:
            return None
        group = self.session_groups.get(name)
        if not group:
            group = SessionGroup(name, weight, set(), 0)
            self.session_groups[name] = group
        return group

    def add_session(self, session):
        self.session_event.set()
        # Return the session groups
        groups = (
            self._session_group(self._ip_addr_group_name(session), 1.0),
        )
        groups = tuple(group for group in groups if group is not None)
        self.sessions[session] = groups
        for group in groups:
            group.sessions.add(session)

    def remove_session(self, session):
        '''Remove a session from our sessions list if there.'''
        self.session_event.set()
        groups = self.sessions.pop(session)
        for group in groups:
            group.retained_cost += session.cost
            group.sessions.remove(session)


class SessionBase(RPCSession):
    '''Base class of ElectrumX JSON sessions.

    Each session runs its tasks in asynchronous parallelism with other
    sessions.
    '''

    MAX_CHUNK_SIZE = 2016
    session_counter = itertools.count()
    log_new = False

    def __init__(
            self,
            session_mgr: 'SessionManager',
            db: 'DB',
            mempool: 'MemPool',
            peer_mgr: 'PeerManager',
            kind: str,
            transport,
    ):
        connection = JSONRPCConnection(JSONRPCAutoDetect)
        super().__init__(transport, connection=connection)
        self.session_mgr = session_mgr
        self.db = db
        self.mempool = mempool
        self.peer_mgr = peer_mgr
        self.kind = kind  # 'RPC', 'TCP' etc.
        self.env = session_mgr.env
        self.coin = self.env.coin
        self.client = 'unknown'
        self.sv_seen = False  # has seen 'server.version' message?
        self.sv_negotiated = asyncio.Event()  # done negotiating protocol version
        self.anon_logs = self.env.anon_logs
        self.txs_sent = 0
        self.log_me = SessionBase.log_new
        self.session_id = None
        self.daemon_request = self.session_mgr.daemon_request
        self.session_id = next(self.session_counter)
        context = {'conn_id': f'{self.session_id}'}
        logger = util.class_logger(__name__, self.__class__.__name__)
        self.logger = util.ConnectionLogger(logger, context)
        self.logger.info(f'{self.kind} {self.remote_address_string()}, '
                         f'{self.session_mgr.session_count():,d} total')
        self.session_mgr.add_session(self)
        self.recalc_concurrency()  # must be called after session_mgr.add_session

    async def notify(
            self,
            *,
            touched_hashxs: Set[bytes],
            touched_outpoints: Set[Tuple[bytes, int]],
            height_changed: bool,
    ):
        pass

    def default_framer(self):
        return NewlineFramer(max_size=self.env.max_recv)

    def remote_address_string(self, *, for_log=True):
        '''Returns the peer's IP address and port as a human-readable
        string, respecting anon logs if the output is for a log.'''
        if for_log and self.anon_logs:
            return 'xx.xx.xx.xx:xx'
        return str(self.remote_address())

    def flags(self):
        '''Status flags.'''
        status = self.kind[0]
        if self.is_closing():
            status += 'C'
        if self.log_me:
            status += 'L'
        status += str(self._incoming_concurrency.max_concurrent)
        return status

    async def connection_lost(self):
        '''Handle client disconnection.'''
        await super().connection_lost()
        self.session_mgr.remove_session(self)
        msg = ''
        if self._incoming_concurrency.max_concurrent < self.initial_concurrent * 0.8:
            msg += ' whilst throttled'
        if self.send_size >= 1_000_000:
            msg += f'.  Sent {self.send_size:,d} bytes in {self.send_count:,d} messages'
        if msg:
            msg = 'disconnected' + msg
            self.logger.info(msg)

    def sub_count_scripthashes(self):
        return 0

    def sub_count_txoutpoints(self):
        return 0

    def sub_count_total(self):
        return self.sub_count_scripthashes() + self.sub_count_txoutpoints()

    async def handle_request(self, request):
        '''Handle an incoming request.  ElectrumX doesn't receive
        notifications from client sessions.
        '''
        if isinstance(request, Request):
            handler = self.request_handlers.get(request.method)
        else:
            handler = None
        method = 'invalid method' if handler is None else request.method

        # Version negotiation must happen before any other messages.
        if not self.sv_seen and method != 'server.version':
            self.logger.info(f'closing session: server.version must be first msg. got: {method}')
            await self._do_crash_old_electrum_client()
            raise ReplyAndDisconnect(BAD_REQUEST, f'use server.version to identify client')
        # Wait for version negotiation to finish before processing other messages.
        if method != 'server.version' and not self.sv_negotiated.is_set():
            await self.sv_negotiated.wait()

        self.session_mgr._method_counts[method] += 1
        coro = handler_invocation(handler, request)()
        return await coro

    async def maybe_crash_old_client(self, ptuple, crash_client_ver):
        if crash_client_ver:
            client_ver = util.protocol_tuple(self.client)
            is_old_protocol = ptuple is None or ptuple <= (1, 2)
            is_old_client = client_ver != (0,) and client_ver <= crash_client_ver
            if is_old_protocol and is_old_client:
                await self._do_crash_old_electrum_client()

    async def _do_crash_old_electrum_client(self):
        self.logger.info(f'attempting to crash old client with version {self.client}')
        # this can crash electrum client 2.6 <= v < 3.1.2
        await self.send_notification('blockchain.relayfee', ())
        # this can crash electrum client (v < 2.8.2) UNION (3.0.0 <= v < 3.3.0)
        await self.send_notification('blockchain.estimatefee', ())


class ElectrumX(SessionBase):
    '''A TCP server that handles incoming Electrum connections.'''

    PROTOCOL_MIN = (1, 4)
    PROTOCOL_MAX = (1, 5)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.subscribe_headers = False
        self.connection.max_response_size = self.env.max_send
        self.hashX_subs = {}  # type: Dict[bytes, bytes]  # hashX -> scripthash
        self.txoutpoint_subs = set()  # type: Set[Tuple[bytes, int]]
        self.mempool_hashX_statuses = {}  # type: Dict[bytes, str]
        self.mempool_txoutpoint_statuses = {}  # type: Dict[Tuple[bytes, int], Mapping[str, Any]]
        self.set_request_handlers(self.PROTOCOL_MIN)
        self.is_peer = False
        self.cost = 5.0   # Connection cost

    @classmethod
    def protocol_min_max_strings(cls):
        return [util.version_string(ver)
                for ver in (cls.PROTOCOL_MIN, cls.PROTOCOL_MAX)]

    @classmethod
    def server_features(cls, env):
        '''Return the server features dictionary.'''
        hosts_dict = {}
        for service in env.report_services:
            port_dict = hosts_dict.setdefault(str(service.host), {})
            if service.protocol not in port_dict:
                port_dict[f'{service.protocol}_port'] = service.port

        min_str, max_str = cls.protocol_min_max_strings()
        return {
            'hosts': hosts_dict,
            'pruning': None,
            'server_version': electrumx.version,
            'protocol_min': min_str,
            'protocol_max': max_str,
            'genesis_hash': env.coin.GENESIS_HASH,
            'hash_function': 'sha256',
            'services': [str(service) for service in env.report_services],
        }

    async def server_features_async(self):
        self.bump_cost(0.2)
        return self.server_features(self.env)

    @classmethod
    def server_version_args(cls):
        '''The arguments to a server.version RPC call to a peer.'''
        return [electrumx.version, cls.protocol_min_max_strings()]

    def protocol_version_string(self):
        return util.version_string(self.protocol_tuple)

    def extra_cost(self):
        return self.session_mgr.extra_cost(self)

    def on_disconnect_due_to_excessive_session_cost(self):
        ip_addr = self.remote_address().host
        groups = self.session_mgr.sessions[self]
        group_names = [group.name for group in groups]
        self.logger.info(f"closing session over res usage. ip: {ip_addr}. groups: {group_names}")

    def sub_count_scripthashes(self):
        return len(self.hashX_subs)

    def sub_count_txoutpoints(self):
        return len(self.txoutpoint_subs)

    def unsubscribe_hashX(self, hashX):
        self.mempool_hashX_statuses.pop(hashX, None)
        return self.hashX_subs.pop(hashX, None)

    async def notify(
            self,
            *,
            touched_hashxs: Set[bytes],
            touched_outpoints: Set[Tuple[bytes, int]],
            height_changed: bool,
    ):
        '''Notify the client about changes to touched addresses (from mempool
        updates or new blocks) and height.
        '''
        # block headers
        if height_changed and self.subscribe_headers:
            args = (await self.subscribe_headers_result(), )
            await self.send_notification('blockchain.headers.subscribe', args)

        # hashXs
        num_hashx_notifs_sent = 0
        touched_hashxs = touched_hashxs.intersection(self.hashX_subs)
        if touched_hashxs or (height_changed and self.mempool_hashX_statuses):
            changed = {}

            for hashX in touched_hashxs:
                alias = self.hashX_subs.get(hashX)
                if alias:
                    status = await self.subscription_address_status(hashX)
                    changed[alias] = status

            # Check mempool hashXs - the status is a function of the confirmed state of
            # other transactions. (this is to detect if height changed from -1 to 0)
            mempool_hashX_statuses = self.mempool_hashX_statuses.copy()
            for hashX, old_status in mempool_hashX_statuses.items():
                alias = self.hashX_subs.get(hashX)
                if alias:
                    status = await self.subscription_address_status(hashX)
                    if status != old_status:
                        changed[alias] = status

            method = 'blockchain.scripthash.subscribe'
            for alias, status in changed.items():
                await self.send_notification(method, (alias, status))
            num_hashx_notifs_sent = len(changed)

        # tx outpoints
        num_txo_notifs_sent = 0
        touched_outpoints = touched_outpoints.intersection(self.txoutpoint_subs)
        if touched_outpoints or (height_changed and self.mempool_txoutpoint_statuses):
            method = 'blockchain.outpoint.subscribe'
            txo_to_status = {}
            for prevout in touched_outpoints:
                txo_to_status[prevout] = await self.txoutpoint_status(*prevout)

            # Check mempool TXOs - the status is a function of the confirmed state of
            # other transactions. (this is to detect if height changed from -1 to 0)
            mempool_txoutpoint_statuses = self.mempool_txoutpoint_statuses.copy()
            for prevout, old_status in mempool_txoutpoint_statuses.items():
                status = await self.txoutpoint_status(*prevout)
                if status != old_status:
                    txo_to_status[prevout] = status

            for tx_hash, txout_idx in touched_outpoints:
                spend_status = txo_to_status[(tx_hash, txout_idx)]
                tx_hash_hex = hash_to_hex_str(tx_hash)
                await self.send_notification(method, ((tx_hash_hex, txout_idx), spend_status))
            num_txo_notifs_sent = len(touched_outpoints)

        if num_hashx_notifs_sent + num_txo_notifs_sent > 0:
            es1 = '' if num_hashx_notifs_sent == 1 else 'es'
            s2 = '' if num_txo_notifs_sent == 1 else 's'
            self.logger.info(f'notified of {num_hashx_notifs_sent:,d} address{es1} and '
                             f'{num_txo_notifs_sent:,d} outpoint{s2}')

    async def subscribe_headers_result(self):
        '''The result of a header subscription or notification.'''
        return self.session_mgr.hsub_results

    async def headers_subscribe(self):
        '''Subscribe to get raw headers of new blocks.'''
        self.subscribe_headers = True
        self.bump_cost(0.25)
        return await self.subscribe_headers_result()

    async def add_peer(self, features):
        '''Add a peer (but only if the peer resolves to the source).'''
        self.is_peer = True
        self.bump_cost(100.0)
        return await self.peer_mgr.on_add_peer(features, self.remote_address())

    async def peers_subscribe(self):
        '''Return the server peers as a list of (ip, host, details) tuples.'''
        self.bump_cost(1.0)
        return self.peer_mgr.on_peers_subscribe(self.is_tor())

    async def address_status(self, hashX: bytes) -> Optional[str]:
        '''Returns an address status, as hex str (or None).'''
        if self.protocol_tuple < (1, 5):
            return await self._address_status_proto_legacy(hashX)
        else:
            return await self._address_status_proto_1_5(hashX)

    async def _address_status_proto_legacy(self, hashX: bytes) -> Optional[str]:
        '''Returns an address status, as per protocol older than <1.5.

        Status is a hex string, but must be None if there is no history.
        '''
        # Note both confirmed history and mempool history are ordered
        # For mempool, height is -1 if it has unconfirmed inputs, otherwise 0
        db_history = await self.limited_history(hashX)
        mempool = await self.mempool.transaction_summaries(hashX)

        status = ''.join(f'{hash_to_hex_str(tx_hash)}:'
                         f'{height:d}:'
                         for tx_hash, height in db_history)
        status += ''.join(f'{hash_to_hex_str(tx.hash)}:'
                          f'{-tx.has_unconfirmed_inputs:d}:'
                          for tx in mempool)

        # Add status hashing cost
        self.bump_cost(0.1 + len(status) * 0.00002)

        if status:
            status = sha256(status.encode()).hex()
        else:
            status = None

        if mempool:
            self.mempool_hashX_statuses[hashX] = status
        else:
            self.mempool_hashX_statuses.pop(hashX, None)

        return status

    async def _calc_intermediate_status_for_hashX(
            self,
            *,
            hashX: bytes,
            txnum_max: int = None,
    ) -> bytes:
        '''Returns the status of a hashX, considering only confirmed history
        up to (<) txnum_max.
        TODO maybe also store intermediate status hashes as part of initial sync? to prep cache...
        '''
        storestatus_period = self.db.history.STORE_INTERMEDIATE_STATUSHASH_EVERY_N_TXS
        reorgsafe_height = self.db.db_height - self.env.reorg_limit
        # get partial status from cache
        tx_num, status = await self.db.history.get_intermediate_statushash_for_hashx(
            hashX=hashX, txnum_max=txnum_max)
        while True:
            # get a history part from db, update status to incorporate it, and maybe store status
            db_history_part = await self.db.limited_history_triples(
                hashX=hashX, limit=storestatus_period, txnum_min=tx_num+1, txnum_max=txnum_max)
            self.bump_cost(0.3 + len(db_history_part) * 0.001)  # cost of history-lookup
            self.bump_cost(36 * len(db_history_part) * 0.00002)  # cost of hashing mined txs
            for (tx_hash, height, tx_num) in db_history_part:
                tx_item = tx_hash + util.pack_le_int32(height)
                status = sha256(status + tx_item)
                if height < reorgsafe_height:
                    self.db.history.store_intermediate_statushash_for_hashx(
                        hashX=hashX, tx_num=tx_num, status=status)
            # if db_history_part is not max-sized, then there are no more parts.
            # (note: even if max-sized, the next part might be empty)
            if len(db_history_part) < storestatus_period:
                return status
            self.logger.info(f"calculated intermediate status for hashX={hashX.hex()}, "
                             f"up to tx_num={tx_num}")

    async def _address_status_proto_1_5(self, hashX: bytes) -> str:
        '''Returns an address status, as per protocol newer than >=1.5'''
        # first, consider confirmed history
        status = await self._calc_intermediate_status_for_hashX(hashX=hashX)

        # second, consider mempool txs
        mempool = await self.mempool.transaction_summaries(hashX)
        self.bump_cost(44 * len(mempool) * 0.00002)  # cost of hashing mempool txs
        for tx in mempool:
            height = -tx.has_unconfirmed_inputs
            tx_item = tx.hash + util.pack_le_int32(height) + util.pack_le_uint64(tx.fee)
            status = sha256(status + tx_item)
        status = status.hex()

        if mempool:
            self.mempool_hashX_statuses[hashX] = status
        else:
            self.mempool_hashX_statuses.pop(hashX, None)

        return status

    async def subscription_address_status(self, hashX):
        '''As for address_status, but if it can't be calculated the subscription is
        discarded.'''
        try:
            return await self.address_status(hashX)
        except RPCError:
            self.unsubscribe_hashX(hashX)
            return None

    async def txoutpoint_status(self, prev_txhash: bytes, txout_idx: int) -> Dict[str, Any]:
        self.bump_cost(0.2)
        spend_status = await self.db.spender_for_txo(prev_txhash, txout_idx)
        if spend_status.spender_height is not None:
            # TXO was created, was mined, was spent, and spend was mined.
            assert spend_status.prev_height > 0
            assert spend_status.spender_height > 0
            assert spend_status.spender_txhash is not None
        else:
            mp_spend_status = await self.mempool.spender_for_txo(prev_txhash, txout_idx)
            if mp_spend_status.prev_height is not None:
                spend_status.prev_height = mp_spend_status.prev_height
            if mp_spend_status.spender_height is not None:
                spend_status.spender_height = mp_spend_status.spender_height
            if mp_spend_status.spender_txhash is not None:
                spend_status.spender_txhash = mp_spend_status.spender_txhash
        # convert to json dict the client expects
        status = {}
        if spend_status.prev_height is not None:
            status['height'] = spend_status.prev_height
            if spend_status.spender_txhash is not None:
                assert spend_status.spender_height is not None
                status['spender_txhash'] = hash_to_hex_str(spend_status.spender_txhash)
                status['spender_height'] = spend_status.spender_height

        prevout = (prev_txhash, txout_idx)
        if ((spend_status.prev_height is not None and spend_status.prev_height <= 0)
                or (spend_status.spender_height is not None and spend_status.spender_height <= 0)):
            self.mempool_txoutpoint_statuses[prevout] = status
        else:
            self.mempool_txoutpoint_statuses.pop(prevout, None)

        return status

    async def hashX_listunspent(self, hashX):
        '''Return the list of UTXOs of a script hash, including mempool
        effects.'''
        utxos = await self.db.all_utxos(hashX)
        utxos = sorted(utxos)
        utxos.extend(await self.mempool.unordered_UTXOs(hashX))
        self.bump_cost(1.0 + len(utxos) / 50)
        spends = await self.mempool.potential_spends(hashX)

        return [{'tx_hash': hash_to_hex_str(utxo.tx_hash),
                 'tx_pos': utxo.tx_pos,
                 'height': utxo.height, 'value': utxo.value}
                for utxo in utxos
                if (utxo.tx_hash, utxo.tx_pos) not in spends]

    async def hashX_subscribe(self, hashX, alias):
        # Store the subscription only after address_status succeeds
        result = await self.address_status(hashX)
        self.hashX_subs[hashX] = alias
        return result

    async def get_balance(self, hashX):
        utxos = await self.db.all_utxos(hashX)
        confirmed = sum(utxo.value for utxo in utxos)
        unconfirmed = await self.mempool.balance_delta(hashX)
        self.bump_cost(1.0 + len(utxos) / 50)
        return {'confirmed': confirmed, 'unconfirmed': unconfirmed}

    async def scripthash_get_balance(self, scripthash):
        '''Return the confirmed and unconfirmed balance of a scripthash.'''
        hashX = scripthash_to_hashX(scripthash)
        return await self.get_balance(hashX)

    async def limited_history(self, hashX):
        '''Returns a sorted list of (tx_hash, height) tuples.
        Raises RPCError if history would not fit within a response.
        '''
        limit_bytes = self.env.max_send - HISTORY_OVER_WIRE_OVERHEAD_BYTES
        limit_nconf = limit_bytes // HISTORY_CONF_ITEM_SIZE_BYTES
        result = await self.db.limited_history(hashX=hashX, limit=limit_nconf)
        self.bump_cost(0.2 + len(result) * 0.001)
        if len(result) >= limit_nconf:
            raise RPCError(BAD_REQUEST, f'history too large')
        return result

    async def unconfirmed_history(self, hashX) -> List[Dict[str, Any]]:
        # Note both confirmed history and mempool history are ordered
        # height is -1 if it has unconfirmed inputs, otherwise 0
        result = [{'tx_hash': hash_to_hex_str(tx.hash),
                   'height': -tx.has_unconfirmed_inputs,
                   'fee': tx.fee}
                  for tx in await self.mempool.transaction_summaries(hashX)]
        self.bump_cost(0.25 + len(result) / 50)
        return result

    async def scripthash_get_history_proto_legacy(self, scripthash):
        '''Return the confirmed and unconfirmed history of a scripthash,
        as per protocol older than <1.5.
        '''
        hashX = scripthash_to_hashX(scripthash)
        history = await self.limited_history(hashX)
        conf = [{'tx_hash': hash_to_hex_str(tx_hash), 'height': height}
                for tx_hash, height in history]
        return conf + await self.unconfirmed_history(hashX)

    async def scripthash_get_history_proto_1_5(
            self,
            scripthash,
            from_height=0,
            to_height=-1,
            client_statushash=None,
            client_height=None
    ):
        '''Return the confirmed and unconfirmed history of a scripthash,
        as per protocol newer than >=1.5.
        '''
        hashX = scripthash_to_hashX(scripthash)
        from_height = non_negative_integer(from_height)
        to_height = integer(to_height)
        if not (-1 <= to_height):
            raise RPCError(BAD_REQUEST, f'{to_height} should be an integer >= -1')
        to_height_or_inf = to_height if to_height >= 0 else float('inf')
        if not (from_height <= to_height_or_inf):
            raise RPCError(BAD_REQUEST, f'from_height={from_height} '
                                        f'<= to_height={to_height} must hold.')
        if (client_statushash is None) != (client_height is None):
            raise RPCError(BAD_REQUEST, f'either both or neither of client_statushash and '
                                        f'client_height must be present')
        if client_statushash is not None:
            client_statushash = assert_status_hash(client_statushash)
            client_height = non_negative_integer(client_height)
            if not (from_height <= client_height < to_height_or_inf):
                raise RPCError(BAD_REQUEST, f'from_height={from_height} '
                                            f'<= client_height={client_height} '
                                            f'< to_height={to_height} must hold.')
        # Done sanitising args; start handling
        # Check if client status is consistent with server; if so we can fast-forward from_height
        if client_statushash is not None:
            client_txnum = self.db.get_next_tx_num_after_blockheight(client_height)
            server_statushash = await self._calc_intermediate_status_for_hashX(
                hashX=hashX,
                txnum_max=client_txnum + 1,
            )
            if server_statushash == client_statushash:
                from_height = client_height + 1

        # Limit size of returned history to ensure it fits within a response.
        # TODO add a min() here so that this won't become "consensus".
        #      or maybe sessions should negotiate max msg size...
        limit_bytes = self.env.max_send - HISTORY_OVER_WIRE_OVERHEAD_BYTES
        limit_nconf = limit_bytes // HISTORY_CONF_ITEM_SIZE_BYTES
        if from_height == 0:
            txnum_min = 0
        else:
            txnum_min = self.db.get_next_tx_num_after_blockheight(from_height - 1)
        if to_height == -1:
            txnum_max = None
        else:
            txnum_max = self.db.get_next_tx_num_after_blockheight(to_height - 1)
        if txnum_min is not None:
            db_history = await self.db.limited_history(
                hashX=hashX,
                limit=limit_nconf,
                txnum_min=txnum_min,
                txnum_max=txnum_max,
            )
        else:
            db_history = []
        self.bump_cost(0.2 + len(db_history) * 0.001)

        if len(db_history) >= limit_nconf:
            # History might have gotten truncated.
            # Note that the truncation might have happened mid-block;
            # hence we need to exclude txs in the last block.
            _, height_last = db_history[-1]
            _, height_first = db_history[0]
            assert height_first < height_last, "history cannot even fit one block of txs"
            db_history = [(tx_hash, height) for (tx_hash, height) in db_history
                          if height != height_last]
            to_height = height_last
        hist_conf = [{'tx_hash': hash_to_hex_str(tx_hash), 'height': height}
                     for tx_hash, height in db_history]
        ret_history = hist_conf
        if to_height == -1:
            # If conf history is long, mempool hist might not fit within response.
            # We either include all mempool txs, or none of them.
            limit_nunconf = ((limit_bytes - len(hist_conf) * HISTORY_CONF_ITEM_SIZE_BYTES)
                             // HISTORY_UNCONF_ITEM_SIZE_BYTES)
            hist_unconf = await self.unconfirmed_history(hashX)
            if len(hist_unconf) <= limit_nunconf:
                ret_history += hist_unconf
            else:
                to_height = max(self.db.db_height + 1, from_height)
        assert (to_height == -1) or (from_height <= to_height)
        return {
            'from_height': from_height,
            'to_height': to_height,
            'history': ret_history,
        }

    async def scripthash_get_mempool(self, scripthash):
        '''Return the mempool transactions touching a scripthash.'''
        hashX = scripthash_to_hashX(scripthash)
        return await self.unconfirmed_history(hashX)

    async def scripthash_listunspent(self, scripthash):
        '''Return the list of UTXOs of a scripthash.'''
        hashX = scripthash_to_hashX(scripthash)
        return await self.hashX_listunspent(hashX)

    async def scripthash_subscribe(self, scripthash):
        '''Subscribe to a script hash.

        scripthash: the SHA256 hash of the script to subscribe to'''
        hashX = scripthash_to_hashX(scripthash)
        return await self.hashX_subscribe(hashX, scripthash)

    async def scripthash_unsubscribe(self, scripthash):
        '''Unsubscribe from a script hash.'''
        self.bump_cost(0.1)
        hashX = scripthash_to_hashX(scripthash)
        return self.unsubscribe_hashX(hashX) is not None

    async def txoutpoint_subscribe(self, tx_hash, txout_idx, spk_hint=None):
        '''Subscribe to an outpoint.

        spk_hint: scriptPubKey corresponding to the outpoint. Might be used by
                  other servers, but we don't need and hence ignore it.
        '''
        tx_hash = assert_tx_hash(tx_hash)
        txout_idx = non_negative_integer(txout_idx)
        if spk_hint is not None:
            assert_hex_str(spk_hint)
        spend_status = await self.txoutpoint_status(tx_hash, txout_idx)
        self.txoutpoint_subs.add((tx_hash, txout_idx))
        return spend_status

    async def txoutpoint_unsubscribe(self, tx_hash, txout_idx):
        '''Unsubscribe from an outpoint.'''
        tx_hash = assert_tx_hash(tx_hash)
        txout_idx = non_negative_integer(txout_idx)
        self.bump_cost(0.1)
        prevout = (tx_hash, txout_idx)
        was_subscribed = prevout in self.txoutpoint_subs
        self.txoutpoint_subs.discard(prevout)
        self.mempool_txoutpoint_statuses.pop(prevout, None)
        return was_subscribed

    async def _merkle_proof(self, cp_height, height):
        max_height = self.db.db_height
        if not height <= cp_height <= max_height:
            raise RPCError(BAD_REQUEST,
                           f'require header height {height:,d} <= '
                           f'cp_height {cp_height:,d} <= '
                           f'chain height {max_height:,d}')
        branch, root = await self.db.header_branch_and_root(cp_height + 1,
                                                            height)
        return {
            'branch': [hash_to_hex_str(elt) for elt in branch],
            'root': hash_to_hex_str(root),
        }

    async def block_header(self, height, cp_height=0):
        '''Return a raw block header as a hexadecimal string, or as a
        dictionary with a merkle proof.'''
        height = non_negative_integer(height)
        cp_height = non_negative_integer(cp_height)
        raw_header_hex = (await self.session_mgr.raw_header(height)).hex()
        self.bump_cost(1.25 - (cp_height == 0))
        if cp_height == 0:
            return raw_header_hex
        result = {'header': raw_header_hex}
        result.update(await self._merkle_proof(cp_height, height))
        return result

    async def block_headers(self, start_height, count, cp_height=0):
        '''Return count concatenated block headers as hex for the main chain;
        starting at start_height.

        start_height and count must be non-negative integers.  At most
        MAX_CHUNK_SIZE headers will be returned.
        '''
        if self.protocol_tuple >= (1, 5):
            return await self.block_headers_array(start_height, count, cp_height)
        start_height = non_negative_integer(start_height)
        count = non_negative_integer(count)
        cp_height = non_negative_integer(cp_height)
        cost = count / 50

        max_size = self.MAX_CHUNK_SIZE
        count = min(count, max_size)
        headers, count = await self.db.read_headers(start_height, count)
        result = {'hex': headers.hex(), 'count': count, 'max': max_size}
        if count and cp_height:
            cost += 1.0
            last_height = start_height + count - 1
            result.update(await self._merkle_proof(cp_height, last_height))
        self.bump_cost(cost)
        return result

    async def block_headers_array(self, start_height, count, cp_height=0):
        '''Return block headers in an array for the main chain;
        starting at start_height.
        start_height and count must be non-negative integers.  At most
        MAX_CHUNK_SIZE headers will be returned.
        '''
        start_height = non_negative_integer(start_height)
        count = non_negative_integer(count)
        cp_height = non_negative_integer(cp_height)
        cost = count / 50

        max_size = self.MAX_CHUNK_SIZE
        count = min(count, max_size)
        headers, count = await self.db.read_headers(start_height, count)
        result = {'count': count, 'max': max_size, 'headers': []}
        if count and cp_height:
            cost += 1.0
            last_height = start_height + count - 1
            result.update(await self._merkle_proof(cp_height, last_height))

        cursor = 0
        height = 0
        while cursor < len(headers):
            next_cursor = self.db.header_offset(height + 1)
            header = headers[cursor:next_cursor]
            result['headers'].append(header.hex())
            cursor = next_cursor
            height += 1

        self.bump_cost(cost)
        return result

    def is_tor(self):
        '''Try to detect if the connection is to a tor hidden service we are
        running.'''
        proxy_address = self.peer_mgr.proxy_address()
        if not proxy_address:
            return False
        return self.remote_address().host == proxy_address.host

    async def replaced_banner(self, banner):
        network_info = await self.daemon_request('getnetworkinfo')
        ni_version = network_info['version']
        major, minor = divmod(ni_version, 1000000)
        minor, revision = divmod(minor, 10000)
        revision //= 100
        daemon_version = f'{major:d}.{minor:d}.{revision:d}'
        for pair in [
                ('$SERVER_VERSION', electrumx.version_short),
                ('$SERVER_SUBVERSION', electrumx.version),
                ('$DAEMON_VERSION', daemon_version),
                ('$DAEMON_SUBVERSION', network_info['subversion']),
                ('$DONATION_ADDRESS', self.env.donation_address),
        ]:
            banner = banner.replace(*pair)
        return banner

    async def donation_address(self):
        '''Return the donation address as a string, empty if there is none.'''
        self.bump_cost(0.1)
        return self.env.donation_address

    async def banner(self):
        '''Return the server banner text.'''
        banner = f'You are connected to an {electrumx.version} server.'
        self.bump_cost(0.5)

        if self.is_tor():
            banner_file = self.env.tor_banner_file
        else:
            banner_file = self.env.banner_file
        if banner_file:
            try:
                with codecs.open(banner_file, 'r', 'utf-8') as f:
                    banner = f.read()
            except (OSError, UnicodeDecodeError) as e:
                self.logger.error(f'reading banner file {banner_file}: {e!r}')
            else:
                banner = await self.replaced_banner(banner)

        return banner

    async def relayfee(self):
        '''The minimum fee a low-priority tx must pay in order to be accepted
        to the daemon's memory pool.'''
        self.bump_cost(1.0)
        return await self.daemon_request('relayfee')

    async def estimatefee(self, number, mode=None):
        '''The estimated transaction fee per kilobyte to be paid for a
        transaction to be included within a certain number of blocks.

        number: the number of blocks
        mode: CONSERVATIVE or ECONOMICAL estimation mode
        '''
        number = non_negative_integer(number)
        # use whitelist for mode, otherwise it would be easy to force a cache miss:
        if mode not in self.coin.ESTIMATEFEE_MODES:
            raise RPCError(BAD_REQUEST, f'unknown estimatefee mode: {mode}')
        self.bump_cost(0.1)

        number = self.coin.bucket_estimatefee_block_target(number)
        cache = self.session_mgr.estimatefee_cache

        cache_item = cache.get((number, mode))
        if cache_item is not None:
            blockhash, feerate, lock = cache_item
            if blockhash and blockhash == self.session_mgr.bp.tip:
                return feerate
        else:
            # create lock now, store it, and only then await on it
            lock = asyncio.Lock()
            cache[(number, mode)] = (None, None, lock)
        async with lock:
            cache_item = cache.get((number, mode))
            if cache_item is not None:
                blockhash, feerate, lock = cache_item
                if blockhash == self.session_mgr.bp.tip:
                    return feerate
            self.bump_cost(2.0)  # cache miss incurs extra cost
            blockhash = self.session_mgr.bp.tip
            if mode:
                feerate = await self.daemon_request('estimatefee', number, mode)
            else:
                feerate = await self.daemon_request('estimatefee', number)
            assert feerate is not None
            assert blockhash is not None
            cache[(number, mode)] = (blockhash, feerate, lock)
            return feerate

    async def ping(self):
        '''Serves as a connection keep-alive mechanism and for the client to
        confirm the server is still responding.
        '''
        self.bump_cost(0.1)
        return None

    async def server_version(self, client_name='', protocol_version=None):
        '''Returns the server version as a string.

        client_name: a string identifying the client
        protocol_version: the protocol version spoken by the client
        '''
        self.bump_cost(0.5)
        if self.sv_seen:
            raise RPCError(BAD_REQUEST, f'server.version already sent')
        self.sv_seen = True

        if client_name:
            client_name = str(client_name)
            if self.env.drop_client is not None and \
                    self.env.drop_client.match(client_name):
                raise ReplyAndDisconnect(RPCError(
                    BAD_REQUEST, f'unsupported client: {client_name}'))
            self.client = client_name[:17]

        # Find the highest common protocol version.  Disconnect if
        # that protocol version in unsupported.
        ptuple, client_min = util.protocol_version(
            protocol_version, self.PROTOCOL_MIN, self.PROTOCOL_MAX)

        await self.maybe_crash_old_client(ptuple, self.env.coin.CRASH_CLIENT_VER)

        if ptuple is None:
            if client_min > self.PROTOCOL_MIN:
                self.logger.info(f'client requested future protocol version '
                                 f'{util.version_string(client_min)} '
                                 f'- is your software out of date?')
            raise ReplyAndDisconnect(RPCError(
                BAD_REQUEST, f'unsupported protocol version: {protocol_version}'))
        self.set_request_handlers(ptuple)

        self.sv_negotiated.set()
        return electrumx.version, self.protocol_version_string()

    async def transaction_broadcast(self, raw_tx):
        '''Broadcast a raw transaction to the network.

        raw_tx: the raw transaction as a hexadecimal string'''
        self.bump_cost(0.25 + len(raw_tx) / 5000)
        # This returns errors as JSON RPC errors, as is natural
        try:
            hex_hash = await self.session_mgr.broadcast_transaction(raw_tx)
        except DaemonError as e:
            error, = e.args
            message = error['message']
            self.logger.info(f'error sending transaction: {message}')
            raise RPCError(BAD_REQUEST, 'the transaction was rejected by '
                           f'network rules.\n\n{message}\n[{raw_tx}]')
        else:
            self.txs_sent += 1
            client_ver = util.protocol_tuple(self.client)
            if client_ver != (0, ):
                msg = self.coin.warn_old_client_on_tx_broadcast(client_ver)
                if msg:
                    self.logger.info(f'sent tx: {hex_hash}. and warned user to upgrade their '
                                     f'client from {self.client}')
                    return msg

            self.logger.info(f'sent tx: {hex_hash}')
            return hex_hash

    async def transaction_get(self, tx_hash, verbose=False):
        '''Return the serialized raw transaction given its hash

        tx_hash: the transaction hash as a hexadecimal string
        verbose: passed on to the daemon
        '''
        tx_hash_bytes = assert_tx_hash(tx_hash)
        tx_hash_hex = tx_hash
        del tx_hash
        if verbose not in (True, False):
            raise RPCError(BAD_REQUEST, '"verbose" must be a boolean')

        self.bump_cost(1.0)

        blockhash = None
        if not self.env.daemon_has_txindex:
            height, tx_pos = await self.db.get_blockheight_and_txpos_for_txhash(tx_hash_bytes)
            if height is not None:
                block_header = self.db.raw_header(height)
                blockhash = self.coin.header_hash(block_header).hex()

        return await self.daemon_request('getrawtransaction', tx_hash_hex, verbose, blockhash)

    async def transaction_merkle(self, tx_hash, height=None):
        '''Return the merkle branch to a confirmed transaction given its hash
        and height.

        tx_hash: the transaction hash as a hexadecimal string
        height: the height of the block it is in
        '''
        tx_hash = assert_tx_hash(tx_hash)
        if height is not None:
            height = non_negative_integer(height)

        height, branch, tx_pos, cost = await self.session_mgr.merkle_branch_for_tx_hash(
            tx_hash=tx_hash, height=height)
        self.bump_cost(cost)

        assert height is not None
        return {"block_height": height, "merkle": branch, "pos": tx_pos}

    async def transaction_id_from_pos(self, height, tx_pos, merkle=False):
        '''Return the txid and optionally a merkle proof, given
        a block height and position in the block.
        '''
        tx_pos = non_negative_integer(tx_pos)
        height = non_negative_integer(height)
        if merkle not in (True, False):
            raise RPCError(BAD_REQUEST, '"merkle" must be a boolean')

        if merkle:
            branch, tx_hash, cost = await self.session_mgr.merkle_branch_for_tx_pos(
                height, tx_pos)
            self.bump_cost(cost)
            return {"tx_hash": tx_hash, "merkle": branch}
        else:
            tx_hashes, cost = await self.session_mgr.tx_hashes_at_blockheight(height)
            try:
                tx_hash = tx_hashes[tx_pos]
            except IndexError:
                raise RPCError(BAD_REQUEST,
                               f'no tx at position {tx_pos:,d} in block at height {height:,d}')
            self.bump_cost(cost)
            return hash_to_hex_str(tx_hash)

    async def compact_fee_histogram(self):
        self.bump_cost(1.0)
        return await self.mempool.compact_fee_histogram()

    def set_request_handlers(self, ptuple):
        self.protocol_tuple = ptuple

        handlers = {
            'blockchain.block.header': self.block_header,
            'blockchain.block.headers': self.block_headers,
            'blockchain.estimatefee': self.estimatefee,
            'blockchain.headers.subscribe': self.headers_subscribe,
            'blockchain.relayfee': self.relayfee,
            'blockchain.scripthash.get_balance': self.scripthash_get_balance,
            'blockchain.scripthash.get_history': self.scripthash_get_history_proto_legacy,
            'blockchain.scripthash.get_mempool': self.scripthash_get_mempool,
            'blockchain.scripthash.listunspent': self.scripthash_listunspent,
            'blockchain.scripthash.subscribe': self.scripthash_subscribe,
            'blockchain.transaction.broadcast': self.transaction_broadcast,
            'blockchain.transaction.get': self.transaction_get,
            'blockchain.transaction.get_merkle': self.transaction_merkle,
            'blockchain.transaction.id_from_pos': self.transaction_id_from_pos,
            'mempool.get_fee_histogram': self.compact_fee_histogram,
            'server.add_peer': self.add_peer,
            'server.banner': self.banner,
            'server.donation_address': self.donation_address,
            'server.features': self.server_features_async,
            'server.peers.subscribe': self.peers_subscribe,
            'server.ping': self.ping,
            'server.version': self.server_version,
        }

        if ptuple >= (1, 4, 2):
            handlers['blockchain.scripthash.unsubscribe'] = self.scripthash_unsubscribe

        if ptuple >= (1, 5):
            handlers['blockchain.outpoint.subscribe'] = self.txoutpoint_subscribe
            handlers['blockchain.outpoint.unsubscribe'] = self.txoutpoint_unsubscribe
            handlers['blockchain.scripthash.get_history'] = self.scripthash_get_history_proto_1_5

        self.request_handlers = handlers


class LocalRPC(SessionBase):
    '''A local TCP RPC server session.'''

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.sv_seen = True
        self.sv_negotiated.set()
        self.client = 'RPC'
        self.connection.max_response_size = 0

    def protocol_version_string(self):
        return 'RPC'


class DashElectrumX(ElectrumX):
    '''A TCP server that handles incoming Electrum Dash connections.'''

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.mns = set()
        self.mn_cache_height = 0
        self.mn_cache = []

    def set_request_handlers(self, ptuple):
        super().set_request_handlers(ptuple)
        self.request_handlers.update({
            'masternode.announce.broadcast':
            self.masternode_announce_broadcast,
            'masternode.subscribe': self.masternode_subscribe,
            'masternode.list': self.masternode_list,
            'protx.diff': self.protx_diff,
            'protx.info': self.protx_info,
        })

    async def notify(
            self,
            *,
            touched_hashxs: Set[bytes],
            touched_outpoints: Set[Tuple[bytes, int]],
            height_changed: bool,
    ):
        '''Notify the client about changes in masternode list.'''
        await super().notify(
            touched_hashxs=touched_hashxs,
            touched_outpoints=touched_outpoints,
            height_changed=height_changed,
        )
        for mn in self.mns.copy():
            status = await self.daemon_request('masternode_list',
                                               ('status', mn))
            await self.send_notification('masternode.subscribe',
                                         (mn, status.get(mn)))

    # Masternode command handlers
    async def masternode_announce_broadcast(self, signmnb):
        '''Pass through the masternode announce message to be broadcast
        by the daemon.

        signmnb: signed masternode broadcast message.'''
        try:
            return await self.daemon_request('masternode_broadcast',
                                             ('relay', signmnb))
        except DaemonError as e:
            error, = e.args
            message = error['message']
            self.logger.info(f'masternode_broadcast: {message}')
            raise RPCError(BAD_REQUEST, 'the masternode broadcast was '
                           f'rejected.\n\n{message}\n[{signmnb}]')

    async def masternode_subscribe(self, collateral):
        '''Returns the status of masternode.

        collateral: masternode collateral.
        '''
        result = await self.daemon_request('masternode_list',
                                           ('status', collateral))
        if result is not None:
            self.mns.add(collateral)
            return result.get(collateral)
        return None

    async def masternode_list(self, payees):
        '''
        Returns the list of masternodes.

        payees: a list of masternode payee addresses.
        '''
        if not isinstance(payees, list):
            raise RPCError(BAD_REQUEST, 'expected a list of payees')

        def get_masternode_payment_queue(mns):
            '''Returns the calculated position in the payment queue for all the
            valid masterernodes in the given mns list.

            mns: a list of masternodes information.
            '''
            now = int(datetime.datetime.utcnow().strftime("%s"))
            mn_queue = []

            # Only ENABLED masternodes are considered for the list.
            for line in mns:
                mnstat = mns[line].split()
                if mnstat[0] == 'ENABLED':
                    # if last paid time == 0
                    if int(mnstat[5]) == 0:
                        # use active seconds
                        mnstat.append(int(mnstat[4]))
                    else:
                        # now minus last paid
                        delta = now - int(mnstat[5])
                        # if > active seconds, use active seconds
                        if delta >= int(mnstat[4]):
                            mnstat.append(int(mnstat[4]))
                        # use active seconds
                        else:
                            mnstat.append(delta)
                    mn_queue.append(mnstat)
            mn_queue = sorted(mn_queue, key=lambda x: x[8], reverse=True)
            return mn_queue

        def get_payment_position(payment_queue, address):
            '''
            Returns the position of the payment list for the given address.

            payment_queue: position in the payment queue for the masternode.
            address: masternode payee address.
            '''
            position = -1
            for pos, mn in enumerate(payment_queue, start=1):
                if mn[2] == address:
                    position = pos
                    break
            return position

        # Accordingly with the masternode payment queue, a custom list
        # with the masternode information including the payment
        # position is returned.
        cache = self.session_mgr.mn_cache
        if not cache or self.session_mgr.mn_cache_height != self.db.db_height:
            full_mn_list = await self.daemon_request('masternode_list',
                                                     ('full',))
            mn_payment_queue = get_masternode_payment_queue(full_mn_list)
            mn_payment_count = len(mn_payment_queue)
            mn_list = []
            for key, value in full_mn_list.items():
                mn_data = value.split()
                mn_info = {
                    'vin': key,
                    'status': mn_data[0],
                    'protocol': mn_data[1],
                    'payee': mn_data[2],
                    'lastseen': mn_data[3],
                    'activeseconds': mn_data[4],
                    'lastpaidtime': mn_data[5],
                    'lastpaidblock': mn_data[6],
                    'ip': mn_data[7]
                }
                mn_info['paymentposition'] = get_payment_position(
                    mn_payment_queue, mn_info['payee']
                )
                mn_info['inselection'] = (
                    mn_info['paymentposition'] < mn_payment_count // 10
                )
                hashX = self.coin.address_to_hashX(mn_info['payee'])
                balance = await self.get_balance(hashX)
                mn_info['balance'] = (sum(balance.values())
                                      / self.coin.VALUE_PER_COIN)
                mn_list.append(mn_info)
            cache.clear()
            cache.extend(mn_list)
            self.session_mgr.mn_cache_height = self.db.db_height

        # If payees is an empty list the whole masternode list is returned
        if payees:
            return [mn for mn in cache if mn['payee'] in payees]
        else:
            return cache

    async def protx_diff(self, base_height, height):
        '''
        Calculates a diff between two deterministic masternode lists.
        The result also contains proof data.

        base_height: The starting block height (starting from 1).
        height: The ending block height.
        '''
        if not isinstance(base_height, int) or not isinstance(height, int):
            raise RPCError(BAD_REQUEST, 'expected a int block heights')

        max_height = self.db.db_height
        if (not 1 <= base_height <= max_height or
                not base_height <= height <= max_height):
            raise RPCError(BAD_REQUEST,
                           f'require 1 <= base_height {base_height:,d} <= '
                           f'height {height:,d} <= '
                           f'chain height {max_height:,d}')

        return await self.daemon_request('protx',
                                         ('diff', base_height, height))

    async def protx_info(self, protx_hash):
        '''
        Returns detailed information about a deterministic masternode.

        protx_hash: The hash of the initial ProRegTx
        '''
        if not isinstance(protx_hash, str):
            raise RPCError(BAD_REQUEST, 'expected protx hash string')

        res = await self.daemon_request('protx', ('info', protx_hash))
        if 'wallet' in res:
            del res['wallet']
        return res


class SmartCashElectrumX(DashElectrumX):
    '''A TCP server that handles incoming Electrum-SMART connections.'''

    def set_request_handlers(self, ptuple):
        super().set_request_handlers(ptuple)
        self.request_handlers.update({
            'smartrewards.current': self.smartrewards_current,
            'smartrewards.check': self.smartrewards_check
        })

    async def smartrewards_current(self):
        '''Returns the current smartrewards info.'''
        result = await self.daemon_request('smartrewards', ('current',))
        if result is not None:
            return result
        return None

    async def smartrewards_check(self, addr):
        '''
        Returns the status of an address

        addr: a single smartcash address
        '''
        result = await self.daemon_request('smartrewards', ('check', addr))
        if result is not None:
            return result
        return None


class AuxPoWElectrumX(ElectrumX):
    async def block_header(self, height, cp_height=0):
        result = await super().block_header(height, cp_height)

        # Older protocol versions don't truncate AuxPoW
        if self.protocol_tuple < (1, 4, 1):
            return result

        # Not covered by a checkpoint; return full AuxPoW data
        if cp_height == 0:
            return result

        # Covered by a checkpoint; truncate AuxPoW data
        result['header'] = self.truncate_auxpow_single(result['header'])
        return result

    async def block_headers(self, start_height, count, cp_height=0):
        # Older protocol versions don't truncate AuxPoW
        if self.protocol_tuple < (1, 4, 1):
            return await super().block_headers(start_height, count, cp_height)

        # Not covered by a checkpoint; return full AuxPoW data
        if cp_height == 0:
            return await super().block_headers(start_height, count, cp_height)

        result = await super().block_headers_array(start_height, count, cp_height)

        # Covered by a checkpoint; truncate AuxPoW data
        result['headers'] = self.truncate_auxpow_headers(result['headers'])

        # Return headers in array form
        if self.protocol_tuple >= (1, 5):
            return result

        # Return headers in concatenated form
        result['hex'] = ''.join(result['headers'])
        del result['headers']
        return result

    def truncate_auxpow_headers(self, headers):
        result = []
        for header in headers:
            result.append(self.truncate_auxpow_single(header))
        return result

    def truncate_auxpow_single(self, header: str):
        # 2 hex chars per byte
        return header[:2*self.coin.TRUNCATED_HEADER_SIZE]
