# Copyright (c) 2016-2018, Neil Booth
#
# All rights reserved.
#
# See the file "LICENCE" for information about the copyright
# and warranty status of this software.

'''Classes for local RPC server and remote client TCP/SSL servers.'''

import asyncio
import codecs
from dataclasses import dataclass
import datetime
import itertools
import math
import os
import ssl
import time
import collections
from collections import defaultdict
from functools import partial
from ipaddress import IPv4Address, IPv6Address, IPv4Network, IPv6Network
from typing import Iterable, Optional, TYPE_CHECKING, Sequence, Union, Any, Tuple, Set, Dict, Mapping
from typing import Callable
import random

import aiorpcx
from aiorpcx import (Event, JSONRPCAutoDetect, JSONRPCConnection,
                     ReplyAndDisconnect, Request, RPCError, RPCSession, Service,
                     handler_invocation, serve_rs, serve_ws, sleep,
                     NewlineFramer, TaskTimeout, timeout_after, run_in_thread,
                     Notification)
from aiorpcx.jsonrpc import SingleRequest

import electrumx
import electrumx.lib.util as util
from electrumx.lib.lrucache import LRUCache
from electrumx.lib.util import OldTaskGroup, is_hex_str
from electrumx.lib.hash import (HASHX_LEN, Base58Error, hash_to_hex_str,
                                hex_str_to_hash, sha256, double_sha256)
from electrumx.lib.merkle import MerkleCache
from electrumx.lib.text import sessions_lines
from electrumx.lib.tx import TXOSpendStatus
from electrumx.server.daemon import DaemonError
from electrumx.server.transport import PaddedRSTransport

if TYPE_CHECKING:
    from electrumx.server.db import DB
    from electrumx.server.env import Env
    from electrumx.server.block_processor import BlockProcessor
    from electrumx.server.daemon import Daemon
    from electrumx.server.mempool import MemPool
    from electrumx.server.peers import PeerManager
    from electrumx.server.controller import Controller, Notifications


BAD_REQUEST = 1
DAEMON_ERROR = 2
RPC_ERROR_HISTORY_TOO_LONG = 10_001


def scripthash_to_hashX(scripthash: str) -> bytes:
    try:
        bin_hash = hex_str_to_hash(scripthash)
        if len(bin_hash) == 32:
            return bin_hash[:HASHX_LEN]
    except (ValueError, TypeError):
        pass
    raise RPCError(BAD_REQUEST, f'{scripthash} is not a valid script hash')


def spk_to_scripthash(spk: str) -> str:
    """Converts scriptPubKey to scripthash."""
    assert_hex_str(spk)
    h = sha256(bytes.fromhex(spk))
    return h[::-1].hex()


def non_negative_integer(value: Any | int) -> int:
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


def assert_boolean(value: Any | bool) -> bool:
    '''Return param value it is boolean otherwise raise an RPCError.'''
    if value in (False, True):
        return value
    raise RPCError(BAD_REQUEST, f'{value} should be a boolean value')


def assert_txid_hum(value: Any | str) -> bytes:
    '''Raise an RPCError if the value is not a valid hexadecimal txid_hum.

    If it is valid, return it as 32-byte binary txid_rev.
    '''
    try:
        raw_hash = hex_str_to_hash(value)
        if len(raw_hash) == 32:
            return raw_hash
    except (ValueError, TypeError):
        pass
    raise RPCError(BAD_REQUEST, f'{value} should be a transaction hash')


def assert_hex_str(value: Any | str, *, allow_odd_len: bool = False) -> None:
    if not is_hex_str(value, allow_odd_len=allow_odd_len):
        raise RPCError(BAD_REQUEST, f'{value} should be a hex string')


def assert_list_or_tuple(value: Any) -> None:
    if not isinstance(value, (list, tuple)):
        raise RPCError(BAD_REQUEST, f'{value} should be a list')


class GracefulDisconnect(Exception):
    pass


@dataclass(slots=True)
class SessionGroup:
    name: str
    weight: float
    sessions: set['SessionBase']
    retained_cost: float

    def session_cost(self) -> float:
        return sum(session.cost for session in self.sessions)

    def cost(self) -> float:
        return self.retained_cost + self.session_cost()


@dataclass(slots=True)
class SessionReferences:
    # All attributes are sets but groups is a list
    sessions: set['SessionBase']
    groups: Sequence['SessionGroup']
    specials: set[str]  # Lower-case strings
    unknown: set[str]


class SessionManager:
    '''Holds global state about all sessions.'''

    def __init__(
            self,
            *,
            env: 'Env',
            db: 'DB',
            block_processor: 'BlockProcessor',
            daemon: 'Daemon',
            mempool: 'MemPool',
            shutdown_event: asyncio.Event,
    ):
        env.max_send = max(350000, env.max_send)
        self.env = env
        self.db = db
        self.bp = block_processor
        self.daemon = daemon
        self.mempool = mempool
        from electrumx.server.peers import PeerManager
        self.peer_mgr = PeerManager(env, db)
        self.shutdown_event = shutdown_event
        self.logger = util.class_logger(__name__, self.__class__.__name__)
        self.servers = {}           # type: Dict[Service, asyncio.Server]
        self.sessions = {}          # type: Dict[SessionBase, Iterable[SessionGroup]]
        self.session_groups = {}    # type: Dict[str, SessionGroup]
        self.txs_sent = 0
        # Would use monotonic time, but aiorpcx sessions use Unix time:
        self.start_time = time.time()
        self._method_counts = defaultdict(int)
        self._reorg_count = 0
        self._history_cache = LRUCache(maxsize=1000)  # type: LRUCache[bytes, Sequence[tuple[bytes, int]] | RPCError]
        self._txids_cache = LRUCache(maxsize=1000)  # type: LRUCache[int, Sequence[bytes]]
        # Really a MerkleCache cache
        self._merkle_txid_cache = LRUCache(maxsize=1000)  # type: LRUCache[int, MerkleCache]
        self.estimatefee_cache: LRUCache[
            tuple[int, str | None],
            tuple[bytes | None, float | None, asyncio.Lock]
        ] = LRUCache(maxsize=1000)
        self.oc_txo_status_cache = LRUCache(maxsize=1000)  # type: LRUCache[tuple[bytes, int], TXOSpendStatus]
        self.notified_height = None
        self.hsub_results = None
        self._task_group = OldTaskGroup()
        self._sslc = None
        # Event triggered when electrumx is listening for incoming requests.
        self.server_listening = Event()
        self.session_event = Event()

        # Set up the RPC request handlers
        cmds = ('add_peer daemon_url disconnect getinfo groups log peers '
                'query reorg sessions stop debug_memusage_list_all_objects '
                'debug_memusage_get_random_backref_chain'.split())
        LocalRPC.request_handlers = {cmd: getattr(self, 'rpc_' + cmd)
                                     for cmd in cmds}

    def _ssl_context(self) -> ssl.SSLContext:
        if self._sslc is None:
            self._sslc = ssl.SSLContext(ssl.PROTOCOL_TLS)
            self._sslc.load_cert_chain(self.env.ssl_certfile, keyfile=self.env.ssl_keyfile)
        return self._sslc

    async def _start_servers(self, services: Iterable[Service]) -> None:
        for service in services:
            kind = service.protocol.upper()
            if service.protocol in self.env.SSL_PROTOCOLS:
                sslc = self._ssl_context()
            else:
                sslc = None
            if service.protocol == 'rpc':
                # local admin RPC
                session_class = LocalRPC
                serve = serve_rs
            else:
                # electrum protocol sessions
                session_class = self.env.coin.SESSIONCLS
                if service.protocol in ('ws', 'wss'):
                    # FIXME also add padding to msgs in websocket sessions
                    serve = serve_ws
                else:
                    serve = partial(serve_rs, transport=PaddedRSTransport)
            # FIXME: pass the service not the kind
            session_factory = partial(
                session_class,
                session_mgr=self,
                db=self.db,
                mempool=self.mempool,
                peer_mgr=self.peer_mgr,
                kind=kind,
            )
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

    async def _stop_servers(self, services: Iterable[Service]):
        '''Stop the servers of the given protocols.'''
        for service in services:
            self.logger.info(f'closing down server for {service}')
            self.servers[service].close()

    def _remove_servers(self, services: Iterable[Service]):
        '''Remove the servers of the given protocols.'''
        for service in services:
            del self.servers[service]

    async def _manage_servers(self) -> None:
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
                services_to_remove = [service for service in self.servers
                                      if service.protocol != 'rpc']
                await self._stop_servers(services_to_remove)
                self._remove_servers(services_to_remove)
                paused = True
            # Start listening for incoming connections if paused and
            # session count has fallen
            if paused and len(self.sessions) <= low_watermark:
                self.logger.info('resuming listening for incoming connections')
                await self._start_external_servers()
                paused = False

    async def _log_sessions(self) -> None:
        '''Periodically log sessions.'''
        log_interval = self.env.log_sessions
        if log_interval:
            while True:
                await sleep(log_interval)
                data = self._session_data(for_log=True)
                for line in sessions_lines(data):
                    self.logger.info(line)
                self.logger.info(util.json_serialize(self._get_info()))

    async def _disconnect_sessions(
            self, sessions: Sequence['SessionBase'], reason: str, *, force_after: float = 1.0,
    ) -> None:
        if sessions:
            session_ids = ', '.join(str(session.session_id) for session in sessions)
            self.logger.info(f'{reason} session ids {session_ids}')
            for session in sessions:
                await self._task_group.spawn(session.close(force_after=force_after))

    async def _clear_stale_sessions(self) -> None:
        '''Cut off sessions that haven't done anything for 10 minutes.'''
        while True:
            await sleep(60)
            stale_cutoff = time.time() - self.env.session_timeout
            stale_sessions = [session for session in self.sessions
                              if session.last_recv < stale_cutoff]
            await self._disconnect_sessions(stale_sessions, 'closing stale')
            del stale_sessions

    async def _handle_chain_reorgs(self) -> None:
        '''Clear certain caches on chain reorgs.'''
        while True:
            await self.bp.backed_up_event.wait()
            self.logger.info(f'reorg signalled; clearing txids and merkle caches')
            self._reorg_count += 1
            # note: history_cache is cleared in _notify_sessions
            # note: txo_status_cache is cleared in _notify_sessions
            self._txids_cache.clear()
            self._merkle_txid_cache.clear()

    async def _recalc_concurrency(self) -> None:
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

    def _get_info(self) -> dict[str, Any]:
        '''A summary of server state.'''
        def cache_fmt(cache: LRUCache):
            return f"{cache.num_lookups} lookups, {cache.num_hits} hits, {len(cache)} entries"
        sessions = self.sessions
        return {
            'caches': {
                'estimatefee': cache_fmt(self.estimatefee_cache),
                'history': cache_fmt(self._history_cache),
                'merkle txid': cache_fmt(self._merkle_txid_cache),
                'txids': cache_fmt(self._txids_cache),
                'txo status': cache_fmt(self.oc_txo_status_cache),
            },
            'coin': self.env.coin.__name__,
            'daemon': self.daemon.logged_url(),
            'daemon height': self.daemon.cached_height(),
            'db height': self.db.db_height,
            'groups': len(self.session_groups),
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
            'txs sent': self.txs_sent,
            'uptime': util.formatted_time(time.time() - self.start_time),
            'version': electrumx.version,
        }

    def _session_data(self, for_log: bool):
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

    async def _refresh_hsub_results(self, height: int) -> None:
        '''Refresh the cached header subscription responses to be for height,
        and record that as notified_height.
        '''
        # Paranoia: a reorg could race and leave db_height lower
        height = min(height, self.db.db_height)
        raw = await self.raw_header(height)
        self.hsub_results = {'hex': raw.hex(), 'height': height}
        self.notified_height = height

    def _session_references(self, items: Iterable[str] | Any, special_strings):
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

    async def rpc_add_peer(self, real_name: str) -> str:
        '''Add a peer.

        real_name: "bch.electrumx.cash t50001 s50002" for example
        '''
        await self.peer_mgr.add_localRPC_peer(real_name)
        return f"peer '{real_name}' added"

    async def rpc_disconnect(self, session_ids: Iterable[str] | Any) -> Sequence[str]:
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

    async def rpc_log(self, session_ids: Iterable[str] | Any) -> Sequence[str]:
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

    async def rpc_daemon_url(self, daemon_url: str):
        '''Replace the daemon URL.'''
        daemon_url = daemon_url or self.env.daemon_url
        try:
            self.daemon.set_url(daemon_url)
        except Exception as e:
            raise RPCError(BAD_REQUEST, f'an error occurred: {e!r}')
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
            history = await db.limited_history(hashX, limit=limit)
            for n, (txid_rev, height) in enumerate(history):
                lines.append(f'History #{n:,d}: height {height:,d} '
                             f'txid {hash_to_hex_str(txid_rev)}')
            if n is None:
                lines.append('No history found')
            n = None
            utxos = await db.all_utxos(hashX)
            for n, utxo in enumerate(utxos, start=1):
                lines.append(f'UTXO #{n:,d}: txid '
                             f'{hash_to_hex_str(utxo.txid_rev)} '
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

    async def rpc_reorg(self, count: int) -> str:
        '''Force a reorg of the given number of blocks.

        count: number of blocks to reorg
        '''
        count = non_negative_integer(count)
        if not self.bp.force_chain_reorg(count):
            raise RPCError(BAD_REQUEST, 'still catching up with daemon')
        return f'scheduled a reorg of {count:,d} blocks'

    async def rpc_debug_memusage_list_all_objects(self, limit: int) -> str:
        """Return a string listing the most common types in memory."""
        import objgraph  # optional dependency
        import io
        with io.StringIO() as fd:
            objgraph.show_most_common_types(
                limit=limit,
                shortnames=False,
                file=fd)
            return fd.getvalue()

    async def rpc_debug_memusage_get_random_backref_chain(self, objtype: str) -> str:
        """Return a dotfile as text containing the backref chain
        for a randomly selected object of type objtype.

        Warning: very slow! and it blocks the server.

        To convert to image:
        $ dot -Tps filename.dot -o outfile.ps
        """
        import objgraph  # optional dependency
        import random
        import io
        with io.StringIO() as fd:
            await run_in_thread(
                lambda:
                objgraph.show_chain(
                    objgraph.find_backref_chain(
                        random.choice(objgraph.by_type(objtype)),
                        objgraph.is_proper_module),
                    output=fd))
            return fd.getvalue()

    # --- External Interface

    async def serve(self, notifications: 'Notifications', event: asyncio.Event) -> None:
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

            self.logger.info(f'max send (response) size {self.env.max_send:,d} bytes')
            self.logger.info(f'max recv (request) size {self.env.max_recv:,d} bytes')
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
            # Stop listening on servers, so no new sessions can be created
            self.logger.info(f'stop listening on servers, so no new sessions can be created')
            await self._stop_servers(self.servers.keys())
            # Then close sessions.
            # note: only best-effort. sessions that are still doing early-handshake are not yet put
            #       into the sessions dict.
            self.logger.info(f'closing {len(self.sessions):,d} active sessions')
            async with OldTaskGroup() as group:
                for session in list(self.sessions):
                    await group.spawn(session.close(force_after=1))
            # Finally, wait for servers to be cleaned up and remove servers
            self.logger.info(f"waiting for all server's resources to close")
            try:
                async with timeout_after(3):
                    async with OldTaskGroup() as group:
                        for server in self.servers.values():
                            await group.spawn(server.wait_closed())
            except TaskTimeout:
                self.logger.warning('timed out waiting for server resources to close')
            servers_to_remove = list(self.servers.keys())
            self._remove_servers(servers_to_remove)

    def extra_cost(self, session: 'SessionBase') -> float:
        # Note there is no guarantee that session is still in self.sessions.  Example traceback:
        # notify_sessions->notify->address_status->bump_cost->recalc_concurrency->extra_cost
        # during which there are many places the sesssion could be removed
        groups = self.sessions.get(session)
        if groups is None:
            return 0
        return sum((group.cost() - session.cost) * group.weight for group in groups)

    async def _merkle_branch(
            self, height: int, txids_rev: Sequence[bytes], tx_pos: int,
    ) -> tuple[Sequence[str], float]:
        tx_count = len(txids_rev)
        cost = tx_count

        if tx_count >= 200:
            self._merkle_txid_cache.num_lookups += 1
            merkle_cache = self._merkle_txid_cache.get(height)
            if merkle_cache:
                self._merkle_txid_cache.num_hits += 1
                cost = 10 * math.sqrt(tx_count)
            else:
                async def tx_hashes_func(start, count):
                    return txids_rev[start: start + count]

                merkle_cache = MerkleCache(self.db.merkle, tx_hashes_func)
                self._merkle_txid_cache[height] = merkle_cache
                await merkle_cache.initialize(len(txids_rev))
            branch, _root = await merkle_cache.branch_and_root(tx_count, tx_pos)
        else:
            branch, _root = self.db.merkle.branch_and_root(txids_rev, tx_pos)

        branch = [hash_to_hex_str(hash) for hash in branch]
        return branch, cost / 2500

    async def merkle_branch_for_txid(
            self, *, txid_rev: bytes, height: int,
    ) -> Tuple[Sequence[str], int, bytes, float]:
        '''Return (branch, tx_pos, block_header, cost).'''
        block_header = await self.raw_header(height)
        txids_rev, txids_cost = await self.txids_rev_at_blockheight(height)
        try:
            tx_pos = txids_rev.index(txid_rev)
        except ValueError:
            raise RPCError(BAD_REQUEST,
                           f'tx {hash_to_hex_str(txid_rev)} not in block at height {height:,d}')
        branch, merkle_cost = await self._merkle_branch(height, txids_rev, tx_pos)
        if block_header != await self.raw_header(height):
            # there was a reorg while processing the request... TODO maybe retry?
            raise RPCError(BAD_REQUEST,
                           f'tx {hash_to_hex_str(txid_rev)} was reorged while processing request')
        return branch, tx_pos, block_header, txids_cost + merkle_cost

    async def merkle_branch_for_tx_pos(self, height: int, tx_pos: int) -> tuple[Sequence[str], str, float]:
        '''Return a triple (branch, txid_hum, cost).'''
        txids_rev, txids_cost = await self.txids_rev_at_blockheight(height)
        try:
            txid_rev = txids_rev[tx_pos]
        except IndexError:
            raise RPCError(BAD_REQUEST,
                           f'no tx at position {tx_pos:,d} in block at height {height:,d}')
        branch, merkle_cost = await self._merkle_branch(height, txids_rev, tx_pos)
        txid_hum = hash_to_hex_str(txid_rev)
        cost = txids_cost + merkle_cost
        return branch, txid_hum, cost

    async def txids_rev_at_blockheight(self, height: int) -> tuple[Sequence[bytes], float]:
        '''Returns a pair (txids_rev, cost).

        txids_rev is an ordered list of binary hashes, cost is an estimated cost of
        getting the hashes; cheaper if in-cache.  Raises RPCError.
        '''
        self._txids_cache.num_lookups += 1
        txids_rev = self._txids_cache.get(height)
        if txids_rev:
            self._txids_cache.num_hits += 1
            return txids_rev, 0.1

        # Ensure the txids_rev are fresh before placing in the cache
        while True:
            reorg_count = self._reorg_count
            try:
                txids_rev = await self.db.txids_rev_at_blockheight(height)
            except self.db.DBError as e:
                raise RPCError(BAD_REQUEST, f'db error: {e!r}')
            if reorg_count == self._reorg_count:
                break

        self._txids_cache[height] = txids_rev

        return txids_rev, 0.25 + len(txids_rev) * 0.0001

    def session_count(self):
        '''The number of connections that we've sent something to.'''
        return len(self.sessions)

    async def daemon_request(self, method: str, *args):
        '''Catch a DaemonError and convert it to an RPCError.'''
        try:
            return await getattr(self.daemon, method)(*args)
        except DaemonError as e:
            raise RPCError(DAEMON_ERROR, f'daemon error: {e!r}') from None

    async def raw_header(self, height: int) -> bytes:
        '''Return the binary header at the given height.'''
        try:
            return await self.db.raw_header(height)
        except IndexError:
            raise RPCError(BAD_REQUEST, f'height {height:,d} '
                           'out of range') from None

    async def broadcast_transaction(self, raw_tx: str) -> str:
        txid_hum = await self.daemon.broadcast_transaction(raw_tx)
        self.txs_sent += 1
        return txid_hum

    async def broadcast_package(self, tx_package: Sequence[str]) -> dict:
        result = await self.daemon.broadcast_package(tx_package)
        self.txs_sent += len(tx_package)
        return result

    async def limited_history(self, hashX: bytes) -> tuple[Sequence[tuple[bytes, int]], float]:
        '''Returns a pair (history, cost).

        History is a sorted list of (txid_rev, height) tuples, or an RPCError.'''
        # History DoS limit.  Each element of history is about 99 bytes when encoded
        # as JSON.
        limit = self.env.max_send // 99
        cost = 0.1
        self._history_cache.num_lookups += 1
        try:
            result = self._history_cache[hashX]
            self._history_cache.num_hits += 1
        except KeyError:
            result = await self.db.limited_history(hashX, limit=limit)
            cost += 0.1 + len(result) * 0.001
            if len(result) >= limit:
                result = RPCError(RPC_ERROR_HISTORY_TOO_LONG, f'history too large', cost=cost)
            self._history_cache[hashX] = result

        assert result is not None
        if isinstance(result, Exception):
            raise result
        return result, cost

    async def _notify_sessions(
            self,
            *,
            touched_hashxs: Set[bytes],
            touched_outpoints: Set[Tuple[bytes, int]],
            height: int,
    ) -> None:
        '''Notify sessions about height changes and touched addresses.'''
        height_changed = height != self.notified_height
        if height_changed:
            await self._refresh_hsub_results(height)
            # Invalidate our history cache for touched hashXs
            for hashX in set(self._history_cache).intersection(touched_hashxs):
                del self._history_cache[hashX]
            # Invalidate our txo-status cache for touched outpoints
            for txo in set(self.oc_txo_status_cache).intersection(touched_outpoints):
                del self.oc_txo_status_cache[txo]

        for session in self.sessions:
            if session.taskgroup.joined:
                continue  # session already being closed, skip it
            # we run this in session.taskgroup, so raising will result in disconnecting just that session:
            coro = session.notify(
                touched_hashxs=touched_hashxs,
                touched_outpoints=touched_outpoints,
                height_changed=height_changed,
            )
            try:
                await session.taskgroup.spawn(coro)
            except RuntimeError:
                if session.taskgroup.joined:
                    pass  # race: task group terminated just after we checked it before spawning
                else:
                    raise

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

    def add_session(self, session: 'SessionBase') -> None:
        self.session_event.set()
        # Return the session groups
        groups = (
            self._session_group(self._ip_addr_group_name(session), 1.0),
        )
        groups = tuple(group for group in groups if group is not None)
        self.sessions[session] = groups
        for group in groups:
            group.sessions.add(session)

    def remove_session(self, session: 'SessionBase') -> None:
        '''Remove a session from our sessions list if there.'''
        self.session_event.set()
        groups = self.sessions.pop(session)
        for group in groups:
            group.retained_cost += session.cost
            group.sessions.remove(session)


class RPCSessionWithTaskGroup(RPCSession):
    def __init__(self, *args, manager_taskgroup: OldTaskGroup, **kwargs):
        RPCSession.__init__(self, *args, **kwargs)
        self._manager_taskgroup = manager_taskgroup
        self.taskgroup = OldTaskGroup()
        asyncio.get_event_loop().create_task(
            self._start_main_loop())

    async def _start_main_loop(self) -> None:
        if self._manager_taskgroup.joined:  # this can happen during shutdown
            self.logger.warning(
                f"manager_taskgroup already terminated. closing session during its init.")
            await self.close(force_after=1.0)
            return
        try:
            await self._manager_taskgroup.spawn(self.main_loop())
        except RuntimeError:
            if self._manager_taskgroup.joined:
                pass  # race: task group terminated just after we checked it before spawning
            else:
                raise

    async def main_loop(self) -> None:
        """Manages taskgroup tied to this session.
        The session and the taskgroup share a lifecycle, either dying will kill the other.
        This method must not raise, to avoid killing the manager_taskgroup.
        """
        self.logger.debug("starting taskgroup.")
        try:
            async with self.taskgroup as group:
                await group.spawn(asyncio.Event().wait)  # run forever (until cancel)
        except GracefulDisconnect as e:
            pass
        except Exception as e:
            self.logger.exception("taskgroup died.")
        finally:
            try:
                await self.close(force_after=1.0)
            except Exception:
                self.logger.exception("unexpected exception while closing session")
            self.logger.debug("taskgroup stopped.")

    async def connection_lost(self):
        """Handle client disconnection."""
        await self.taskgroup.cancel_remaining()
        await super().connection_lost()


class SessionBase(RPCSessionWithTaskGroup):
    '''Base class of ElectrumX JSON sessions.

    Each session runs its tasks in asynchronous parallelism with other
    sessions.
    '''

    MAX_CHUNK_SIZE = 2016
    session_counter = itertools.count()
    log_new = False
    request_handlers: Dict[str, Callable]
    notification_handlers: Dict[str, Callable]

    def __init__(
            self,
            transport,
            *,
            session_mgr: 'SessionManager',
            db: 'DB',
            mempool: 'MemPool',
            peer_mgr: 'PeerManager',
            kind: str,
    ):
        connection = JSONRPCConnection(JSONRPCAutoDetect)
        super().__init__(
            transport,
            manager_taskgroup=session_mgr._task_group,
            connection=connection,
        )
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
        self.session_id = None  # type: int
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
    ) -> None:
        pass

    def default_framer(self):
        return NewlineFramer(max_size=self.env.max_recv)

    def remote_address_string(self, *, for_log: bool = True) -> str:
        '''Returns the peer's IP address and port as a human-readable
        string, respecting anon logs if the output is for a log.'''
        if for_log and self.anon_logs:
            return 'xx.xx.xx.xx:xx'
        return str(self.remote_address())

    def flags(self) -> str:
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

    def sub_count_scripthashes(self) -> int:
        return 0

    def sub_count_txoutpoints(self) -> int:
        return 0

    def sub_count_total(self) -> int:
        return self.sub_count_scripthashes() + self.sub_count_txoutpoints()

    async def handle_request(self, request: SingleRequest):
        '''Handle an incoming request.'''
        handler = None
        if isinstance(request, Request):
            handler = self.request_handlers.get(request.method)
        elif isinstance(request, Notification):
            handler = self.notification_handlers.get(request.method)
        method = 'invalid method' if handler is None else request.method

        # Version negotiation must happen before any other messages.
        if not self.sv_seen and method != 'server.version':
            self.logger.info(f'closing session: server.version must be first msg. got: {method}')
            await self._do_crash_old_electrum_client()
            raise ReplyAndDisconnect(RPCError(
                BAD_REQUEST, f'use server.version to identify client'))
        # Wait for version negotiation to finish before processing other messages.
        if method != 'server.version' and not self.sv_negotiated.is_set():
            await self.sv_negotiated.wait()

        self.session_mgr._method_counts[method] += 1
        coro = handler_invocation(handler, request)()
        return await coro

    def protocol_version_string(self) -> str:
        raise NotImplementedError()

    async def maybe_crash_old_client(
            self, ptuple: Optional[tuple[int, ...]], crash_client_ver: Optional[tuple[int, ...]],
    ) -> None:
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
    # consider bumping Coin.MIN_REQUIRED_DAEMON_VERSION too when releasing a new protocol version
    PROTOCOL_MAX = (1, 6, 0)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.subscribe_headers = False
        self.connection.max_response_size = self.env.max_send
        self.hashX_subs = {}  # type: Dict[bytes, str]  # hashX -> scripthash
        self.txoutpoint_subs = set()  # type: Set[Tuple[bytes, int]]  # (txid_rev, txout_idx)
        self.mempool_hashX_statuses = {}  # type: Dict[bytes, str]
        self.mempool_txoutpoint_statuses = {}  # type: Dict[Tuple[bytes, int], TXOSpendStatus]
        self.set_request_handlers(self.PROTOCOL_MIN)
        self.is_peer = False
        self.cost = 5.0   # Connection cost

    @classmethod
    def protocol_min_max_strings(cls) -> tuple[str, str]:
        return tuple(
            util.version_string(ver)
            for ver in (cls.PROTOCOL_MIN, cls.PROTOCOL_MAX))

    @classmethod
    def server_features(cls, env: 'Env') -> dict[str, Any]:
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
            'hash_function': 'sha256',  # FIXME should only be present for proto < 1.7
            'services': [str(service) for service in env.report_services],
        }

    async def phandle_server_features_async(self) -> dict[str, Any]:
        self.bump_cost(0.2)
        features = self.server_features(self.env)
        if self.protocol_tuple >= (1, 7):
            features.pop('hash_function', None)
        return features

    @classmethod
    def server_version_args(cls):
        '''The arguments to a server.version RPC call to a peer.'''
        return [electrumx.version, cls.protocol_min_max_strings()]

    def protocol_version_string(self):
        return util.version_string(self.protocol_tuple)

    def extra_cost(self) -> float:
        return self.session_mgr.extra_cost(self)

    def on_disconnect_due_to_excessive_session_cost(self):
        remote_addr = self.remote_address()
        ip_addr = remote_addr.host if remote_addr else None
        groups = self.session_mgr.sessions[self]
        group_names = [group.name for group in groups]
        self.logger.info(f"closing session over res usage. ip: {ip_addr}. groups: {group_names}")

    def sub_count_scripthashes(self):
        return len(self.hashX_subs)

    def sub_count_txoutpoints(self):
        return len(self.txoutpoint_subs)

    def unsubscribe_hashX(self, hashX: bytes) -> Optional[str]:
        self.mempool_hashX_statuses.pop(hashX, None)
        return self.hashX_subs.pop(hashX, None)

    async def notify(
            self,
            *,
            touched_hashxs: Set[bytes],
            touched_outpoints: Set[Tuple[bytes, int]],
            height_changed: bool,
    ):
        """Send notifications.
        If we raise, we will disconnect from just this session.
        (websockets raise exceptions for unclear reasons?)
        """
        try:
            async with timeout_after(30):
                await self._notify_inner(
                    touched_hashxs=touched_hashxs,
                    touched_outpoints=touched_outpoints,
                    height_changed=height_changed,
                )
        except TaskTimeout as e:
            self.logger.warning(
                f"timeout notifying client, closing... "
                f"sub_count: sh={self.sub_count_scripthashes()}, txo={self.sub_count_txoutpoints()}."
            )
            raise GracefulDisconnect from e
        except RPCError as e:
            self.logger.warning(
                f"RPCError while notifying client, closing... "
                f"sub_count: sh={self.sub_count_scripthashes()}, txo={self.sub_count_txoutpoints()}. "
                f"RPCError: {e}"
            )
            raise GracefulDisconnect from e

    async def _notify_inner(
            self,
            *,
            touched_hashxs: Set[bytes],
            touched_outpoints: Set[Tuple[bytes, int]],
            height_changed: bool,
    ) -> None:
        '''Notify the client about changes to touched addresses (from mempool
        updates or new blocks) and height.
        '''
        cnt_sent = 0
        # block headers
        if height_changed and self.subscribe_headers:
            args = (await self.subscribe_headers_result(), )
            await self.send_notification('blockchain.headers.subscribe', args)
            cnt_sent += 1

        # hashXs
        num_hashx_notifs_sent = 0
        touched_hashxs = touched_hashxs.intersection(self.hashX_subs)
        if touched_hashxs or (height_changed and self.mempool_hashX_statuses):
            changed = {}  # type: dict[str, Optional[str]]

            for hashX in touched_hashxs:
                scripthash = self.hashX_subs.get(hashX)
                if scripthash:
                    status = await self.subscription_address_status(hashX)
                    changed[scripthash] = status

            # Check mempool hashXs - the status is a function of the confirmed state of
            # other transactions. (this is to detect if height changed from -1 to 0)
            mempool_hashX_statuses = self.mempool_hashX_statuses.copy()
            for hashX, old_status in mempool_hashX_statuses.items():
                scripthash = self.hashX_subs.get(hashX)
                if scripthash:
                    status = await self.subscription_address_status(hashX)
                    if status != old_status:
                        changed[scripthash] = status

            if self.protocol_tuple >= (1, 7):
                method = 'blockchain.scriptpubkey.subscribe'
            else:
                method = 'blockchain.scripthash.subscribe'
            for scripthash, status in changed.items():
                await self.send_notification(method, (scripthash, status))
                cnt_sent += 1
            num_hashx_notifs_sent = len(changed)

        # tx outpoints
        num_txo_notifs_sent = 0
        touched_outpoints = touched_outpoints.intersection(self.txoutpoint_subs)
        if touched_outpoints or (height_changed and self.mempool_txoutpoint_statuses):
            method = 'blockchain.outpoint.subscribe'
            txo_to_status = {}  # type: dict[tuple[bytes, int], TXOSpendStatus]
            for prevout in touched_outpoints:
                txo_to_status[prevout] = await self.txoutpoint_status_for_notif(*prevout)  # can raise RPCError

            # Check mempool TXOs - the status is a function of the confirmed state of
            # other transactions. (this is to detect if height changed from -1 to 0)
            mempool_txoutpoint_statuses = self.mempool_txoutpoint_statuses.copy()
            for prevout, old_status in mempool_txoutpoint_statuses.items():
                status = await self.txoutpoint_status_for_notif(*prevout)  # can raise RPCError
                if status != old_status:
                    txo_to_status[prevout] = status

            for txid_rev, txout_idx in touched_outpoints:
                spend_status = txo_to_status[(txid_rev, txout_idx)]
                spend_status_dict = self._convert_txospendstatus_to_protocol_dict(spend_status)
                tx_hash_hex = hash_to_hex_str(txid_rev)
                await self.send_notification(method, (tx_hash_hex, txout_idx, spend_status_dict))
                cnt_sent += 1
            num_txo_notifs_sent = len(touched_outpoints)

        # log (number of useful notifications we sent)
        if num_hashx_notifs_sent + num_txo_notifs_sent > 0:
            es1 = '' if num_hashx_notifs_sent == 1 else 'es'
            s2 = '' if num_txo_notifs_sent == 1 else 's'
            self.logger.info(f'notified of {num_hashx_notifs_sent:,d} address{es1} and '
                             f'{num_txo_notifs_sent:,d} outpoint{s2}')

        # maybe send some noise
        if self.protocol_tuple >= (1, 7):
            if height_changed:  # on block
                if cnt_sent < 2:
                    await self.send_ping_notification_to_client(data_len=128)  # similar len to bc.spk.sub
                while random.random() < 0.1:
                    await self.send_ping_notification_to_client(data_len=128)
            else:  # on mempool
                once_per_10_minutes = self.mempool.refresh_secs / 600
                if random.random() < once_per_10_minutes:
                    await self.send_ping_notification_to_client(data_len=128)

    async def subscribe_headers_result(self):
        '''The result of a header subscription or notification.'''
        return self.session_mgr.hsub_results

    async def phandle_headers_subscribe(self):
        '''Subscribe to get raw headers of new blocks.'''
        self.subscribe_headers = True
        self.bump_cost(0.25)
        return await self.subscribe_headers_result()

    async def phandle_add_peer(self, features: dict[str, Any] | Any):
        '''Add a peer (but only if the peer resolves to the source).'''
        self.is_peer = True
        self.bump_cost(100.0)
        return await self.peer_mgr.on_add_peer(features, self.remote_address())

    async def phandle_peers_subscribe(self):
        '''Return the server peers as a list of (ip, host, details) tuples.'''
        self.bump_cost(1.0)
        return self.peer_mgr.on_peers_subscribe(self.is_tor())

    async def address_status(self, hashX: bytes) -> Optional[str]:
        '''Returns an address status.

        Status is a hex string, but must be None if there is no history.
        Can raise RPCError.
        Side-effect: updates client-last-seen status, used by notifications.
        '''
        # Note both confirmed history and mempool history are ordered
        # For mempool, height is -1 if it has unconfirmed inputs, otherwise 0
        db_history, cost = await self.session_mgr.limited_history(hashX)
        mempool = await self.mempool.transaction_summaries(hashX)

        status = ''.join(f'{hash_to_hex_str(tx_hash)}:'
                         f'{height:d}:'
                         for tx_hash, height in db_history)
        status += ''.join(f'{hash_to_hex_str(tx.txid_rev)}:'
                          f'{-tx.has_unconfirmed_inputs:d}:'
                          for tx in mempool)

        # Add status hashing cost
        self.bump_cost(cost + 0.1 + len(status) * 0.00002)

        if status:
            status = sha256(status.encode()).hex()
        else:
            status = None

        # update status last sent to client
        if mempool:
            self.mempool_hashX_statuses[hashX] = status
        else:
            self.mempool_hashX_statuses.pop(hashX, None)

        return status

    async def subscription_address_status(self, hashX: bytes) -> Optional[str]:
        '''As for address_status, but if it can't be calculated the subscription is
        discarded.'''
        try:
            return await self.address_status(hashX)
        except RPCError:
            self.unsubscribe_hashX(hashX)
            return None

    async def _calc_oc_txo_status(self, funder_txid_rev: bytes, txout_idx: int) -> 'TXOSpendStatus':
        """For an outpoint, returns its spend-status (ignoring mempool events).

        Uses daemon (bitcoind) to find the spender_txhash, requiring "txospenderindex=1".
        However, mempool events are ignored, as it would be difficult to distinguish block height 0 vs -1
        using only the daemon. Instead, our own mempool data (as opposed to bitcoind's) can be used
        separately to enrich the return value.

        Can raise RPCError.
        """
        funder_txid_hum = hash_to_hex_str(funder_txid_rev)
        # 1. call bitcoind "getrawtransaction" to see if prevtx exists/is_mined
        self.bump_cost(1)
        try:
            funder_item = await self.session_mgr.daemon.getrawtransaction(funder_txid_hum, verbose=True)  # verbose=int(1)
        except DaemonError as e:
            error, = e.args
            ecode = error['code']
            if ecode == -5:  # "No such mempool or blockchain transaction."
                return TXOSpendStatus(funder_height=None)  # utxo never existed
            self.logger.debug(f"getrawtransaction errored. {funder_txid_hum=}. {error=}")
            raise RPCError(DAEMON_ERROR, f'daemon error: {error!r}') from None
        assert funder_item.get("txid") == funder_txid_hum, f"{funder_item.get('txid')=} != {funder_txid_hum=}"
        funder_bhash = funder_item.get("blockhash")
        funder_bheight = None  # type: Optional[int]
        if funder_bhash is not None:
            funder_bheight = self.db.get_blockheight_from_blockhash(funder_bhash)
        if funder_bheight is None:  # if in mempool, will defer to mempool.spender_for_txo
            return TXOSpendStatus(funder_height=None)  # utxo never existed (in chain)
        assert isinstance(funder_bheight, int)
        # ok, funding tx exists, does the requested output index also exist in this tx?
        vouts = funder_item.get("vout") or []
        if len(vouts) <= txout_idx:
            return TXOSpendStatus(funder_height=None)  # txout_idx was out-of-bounds
        # by now we know the funding TXO existed in the chain. Let's see if it was spent.
        # 2. call bitcoind "gettxspendingprevout"
        self.bump_cost(1)
        try:
            spender_item = await self.session_mgr.daemon.gettxspendingprevout(funder_txid_hum, txout_idx)
        except DaemonError as e:
            error, = e.args
            self.logger.debug(f"gettxspendingprevout errored. txo={funder_txid_hum}:{txout_idx}. {error=}")
            raise RPCError(DAEMON_ERROR, f'daemon error: {error!r}') from None
        assert spender_item.get("txid") == funder_txid_hum, f"{spender_item.get('txid')=} != {funder_txid_hum=}"
        # paranoia: check if funder tx got reorged while we were awaiting bitcoind RPC
        if self.db.get_blockheight_from_blockhash(funder_bhash) is None:
            raise RPCError(BAD_REQUEST,
                           f'tx {funder_txid_hum} was reorged while processing request')  # TODO maybe retry?
        spender_bhash = spender_item.get("blockhash")
        spender_bheight = None
        if spender_bhash is not None:
            spender_bheight = self.db.get_blockheight_from_blockhash(spender_bhash)
        if spender_bheight is None:  # if in mempool, will defer to mempool.spender_for_txo
            return TXOSpendStatus(funder_height=funder_bheight)  # utxo funded but unspent (in-chain)
        spender_txid = spender_item.get("spendingtxid")
        assert spender_txid is not None  # we already have a height!
        # utxo funded, and spent (in-chain)
        return TXOSpendStatus(
            funder_height=funder_bheight,
            spender_txid_rev=hex_str_to_hash(spender_txid),
            spender_height=spender_bheight,
        )

    def _convert_txospendstatus_to_protocol_dict(self, spend_status: 'TXOSpendStatus') -> dict[str, Any]:
        # convert to json dict the client expects
        d = {}
        if spend_status.funder_height is not None:
            d['funder_height'] = spend_status.funder_height
            if spend_status.spender_txid_rev is not None:
                assert spend_status.spender_height is not None
                d['spender_txhash'] = hash_to_hex_str(spend_status.spender_txid_rev)
                d['spender_height'] = spend_status.spender_height
        return d

    async def _calc_txoutpoint_status(self, prev_txid_rev: bytes, txout_idx: int) -> 'TXOSpendStatus':
        """Can raise RPCError"""
        self.bump_cost(0.1)
        prevout = (prev_txid_rev, txout_idx)
        # first, consider only on-chain mined events, and check cache first (to avoid bitcoind RPC calls)
        self.session_mgr.oc_txo_status_cache.num_lookups += 1
        try:
            oc_status = self.session_mgr.oc_txo_status_cache[prevout]
            self.session_mgr.oc_txo_status_cache.num_hits += 1
        except KeyError:
            oc_status = await self._calc_oc_txo_status(prev_txid_rev, txout_idx)  # "on-chain" status
            self.session_mgr.oc_txo_status_cache[prevout] = oc_status
        assert oc_status is not None
        # let's see if we also need to consider the mempool
        if oc_status.spender_height is not None:
            # TXO was created, was mined, was spent, and spend was mined.
            assert oc_status.funder_height > 0
            assert oc_status.spender_height > 0
            assert oc_status.spender_txid_rev is not None
            ret = oc_status
        else:  # mempool is still relevant
            self.bump_cost(0.1)
            mp_status = await self.mempool.spender_for_txo(prev_txid_rev, txout_idx)
            ret = TXOSpendStatus(
                funder_height=mp_status.funder_height if mp_status.funder_height is not None else oc_status.funder_height,
                spender_txid_rev=mp_status.spender_txid_rev if mp_status.spender_txid_rev is not None else oc_status.spender_txid_rev,
                spender_height=mp_status.spender_height if mp_status.spender_height is not None else oc_status.spender_height,
            )
        return ret

    async def txoutpoint_status_for_notif(self, prev_txid_rev: bytes, txout_idx: int) -> 'TXOSpendStatus':
        """Can raise RPCError
        Side-effect: updates client-last-seen status, used by notifications.
        """
        status = await self._calc_txoutpoint_status(prev_txid_rev=prev_txid_rev, txout_idx=txout_idx)
        # update status last sent to client
        prevout = (prev_txid_rev, txout_idx)
        fh = status.funder_height
        sh = status.spender_height
        if ((fh is not None and fh <= 0)
                or (sh is not None and sh <= 0)):
            self.mempool_txoutpoint_statuses[prevout] = status
        else:
            self.mempool_txoutpoint_statuses.pop(prevout, None)
        return status

    async def hashX_listunspent(self, hashX: bytes) -> Sequence[dict[str, Any]]:
        '''Return the list of UTXOs of a script hash, including mempool
        effects.'''
        utxos = await self.db.all_utxos(hashX)
        utxos = sorted(utxos)
        utxos.extend(await self.mempool.unordered_UTXOs(hashX))
        self.bump_cost(1.0 + len(utxos) / 50)
        spends = await self.mempool.potential_spends(hashX)

        return [{'tx_hash': hash_to_hex_str(utxo.txid_rev),
                 'tx_pos': utxo.tx_pos,
                 'height': utxo.height, 'value': utxo.value}
                for utxo in utxos
                if (utxo.txid_rev, utxo.tx_pos) not in spends]

    async def hashX_subscribe(self, hashX: bytes, scripthash: str) -> Optional[str]:
        # Store the subscription only after address_status succeeds
        result = await self.address_status(hashX)
        self.hashX_subs[hashX] = scripthash
        return result

    async def get_balance(self, hashX: bytes) -> dict[str, Any]:
        utxos = await self.db.all_utxos(hashX)
        confirmed = sum(utxo.value for utxo in utxos)
        unconfirmed = await self.mempool.balance_delta(hashX)
        self.bump_cost(1.0 + len(utxos) / 50)
        return {'confirmed': confirmed, 'unconfirmed': unconfirmed}

    async def phandle_scripthash_get_balance(self, scripthash: str | Any) -> dict[str, Any]:
        '''Return the confirmed and unconfirmed balance of a scripthash.'''
        hashX = scripthash_to_hashX(scripthash)
        return await self.get_balance(hashX)

    async def unconfirmed_history(self, hashX: bytes) -> list[dict[str, Any]]:
        # Note both confirmed history and mempool history are ordered
        # height is -1 if it has unconfirmed inputs, otherwise 0
        result = [{'tx_hash': hash_to_hex_str(tx.txid_rev),
                   'height': -tx.has_unconfirmed_inputs,
                   'fee': tx.fee}
                  for tx in await self.mempool.transaction_summaries(hashX)]
        self.bump_cost(0.25 + len(result) / 50)
        return result

    async def confirmed_and_unconfirmed_history(self, hashX: bytes) -> list[dict[str, Any]]:
        # Note both confirmed history and mempool history are ordered
        history, cost = await self.session_mgr.limited_history(hashX)
        self.bump_cost(cost)
        conf = [{'tx_hash': hash_to_hex_str(txid_rev), 'height': height}
                for txid_rev, height in history]
        return conf + await self.unconfirmed_history(hashX)

    async def phandle_scripthash_get_history(self, scripthash: str | Any) -> list[dict[str, Any]]:
        '''Return the confirmed and unconfirmed history of a scripthash.'''
        hashX = scripthash_to_hashX(scripthash)
        return await self.confirmed_and_unconfirmed_history(hashX)

    async def phandle_scripthash_get_mempool(self, scripthash: str | Any) -> list[dict[str, Any]]:
        '''Return the mempool transactions touching a scripthash.'''
        hashX = scripthash_to_hashX(scripthash)
        return await self.unconfirmed_history(hashX)

    async def phandle_scripthash_listunspent(self, scripthash: str | Any) -> Sequence[dict[str, Any]]:
        '''Return the list of UTXOs of a scripthash.'''
        hashX = scripthash_to_hashX(scripthash)
        return await self.hashX_listunspent(hashX)

    async def phandle_scripthash_subscribe(self, scripthash: str | Any) -> Optional[str]:
        '''Subscribe to a script hash.

        scripthash: the SHA256 hash of the script to subscribe to'''
        hashX = scripthash_to_hashX(scripthash)
        return await self.hashX_subscribe(hashX, scripthash)

    async def phandle_scripthash_unsubscribe(self, scripthash: str | Any):
        '''Unsubscribe from a script hash.'''
        self.bump_cost(0.1)
        hashX = scripthash_to_hashX(scripthash)
        return self.unsubscribe_hashX(hashX) is not None

    def phandle_scriptpubkey_get_balance(self, spk: str) -> collections.abc.Awaitable[dict]:
        scripthash = spk_to_scripthash(spk)
        return self.phandle_scripthash_get_balance(scripthash)

    def phandle_scriptpubkey_get_history(self, spk: str) -> collections.abc.Awaitable[list]:
        scripthash = spk_to_scripthash(spk)
        return self.phandle_scripthash_get_history(scripthash)

    def phandle_scriptpubkey_get_mempool(self, spk: str) -> collections.abc.Awaitable[list]:
        scripthash = spk_to_scripthash(spk)
        return self.phandle_scripthash_get_mempool(scripthash)

    def phandle_scriptpubkey_listunspent(self, spk: str) -> collections.abc.Awaitable[list]:
        scripthash = spk_to_scripthash(spk)
        return self.phandle_scripthash_listunspent(scripthash)

    def phandle_scriptpubkey_subscribe(self, spk: str) -> collections.abc.Awaitable[Optional[str]]:
        scripthash = spk_to_scripthash(spk)
        return self.phandle_scripthash_subscribe(scripthash)

    def phandle_scriptpubkey_unsubscribe(self, spk: str) -> collections.abc.Awaitable[bool]:
        scripthash = spk_to_scripthash(spk)
        return self.phandle_scripthash_unsubscribe(scripthash)

    async def phandle_txoutpoint_get_status(self, tx_hash: str | Any, txout_idx: int | Any, spk_hint: str | Any) -> dict[str, Any]:
        '''Return the status of an outpoint, without subscribing.

        spk_hint: scriptPubKey corresponding to the outpoint. Might be used by
                  other servers, but we don't need and hence ignore it.
        '''
        txid_rev = assert_txid_hum(tx_hash)
        txout_idx = non_negative_integer(txout_idx)
        assert_hex_str(spk_hint)
        # calc status (but do not side-effect client-last-seen status)
        spend_status = await self._calc_txoutpoint_status(txid_rev, txout_idx)
        d = self._convert_txospendstatus_to_protocol_dict(spend_status)
        return d

    async def phandle_txoutpoint_subscribe(self, tx_hash: str | Any, txout_idx: int | Any, spk_hint: str | Any) -> dict[str, Any]:
        '''Subscribe to an outpoint.

        spk_hint: scriptPubKey corresponding to the outpoint. Might be used by
                  other servers, but we don't need and hence ignore it.
        '''
        txid_rev = assert_txid_hum(tx_hash)
        txout_idx = non_negative_integer(txout_idx)
        assert_hex_str(spk_hint)
        # calc status, update client-last-seen status.
        # if we can't calc the status, as e.g. bitcoind errors, don't add the subscription
        spend_status = await self.txoutpoint_status_for_notif(txid_rev, txout_idx)
        # sub to outpoint
        self.txoutpoint_subs.add((txid_rev, txout_idx))
        d = self._convert_txospendstatus_to_protocol_dict(spend_status)
        return d

    async def phandle_txoutpoint_unsubscribe(self, tx_hash: str | Any, txout_idx: int | Any) -> bool:
        '''Unsubscribe from an outpoint.'''
        txid_rev = assert_txid_hum(tx_hash)
        txout_idx = non_negative_integer(txout_idx)
        self.bump_cost(0.1)
        prevout = (txid_rev, txout_idx)
        was_subscribed = prevout in self.txoutpoint_subs
        self.txoutpoint_subs.discard(prevout)
        self.mempool_txoutpoint_statuses.pop(prevout, None)
        return was_subscribed

    async def _merkle_proof(self, cp_height: int, height: int) -> dict[str, Any]:
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

    async def phandle_block_header(self, height: int, cp_height: int = 0) -> dict[str, Any]:
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

    async def phandle_block_headers(self, start_height: int, count: int, cp_height: int = 0) -> dict[str, Any]:
        '''Return count concatenated block headers as hex for the main chain;
        starting at start_height.

        start_height and count must be non-negative integers.  At most
        MAX_CHUNK_SIZE headers will be returned.
        '''
        if self.protocol_tuple >= (1, 6):
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

    async def block_headers_array(self, start_height: int, count: int, cp_height: int = 0) -> dict[str, Any]:
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

    def is_tor(self) -> bool:
        '''Try to detect if the connection is to a tor hidden service we are
        running.'''
        proxy_address = self.peer_mgr.proxy_address()
        if not proxy_address:
            return False
        remote_addr = self.remote_address()
        if not remote_addr:
            return False
        return remote_addr.host == proxy_address.host

    async def replaced_banner(self, banner: str) -> str:
        network_info = await self.daemon_request('getnetworkinfo')
        ni_version = network_info['version']  # e.g. 290100 (for /Satoshi:29.1.0/)
        major = ni_version // 10_000
        minor = (ni_version % 10_000) // 100
        revision = ni_version % 100
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

    async def phandle_donation_address(self) -> str:
        '''Return the donation address as a string, empty if there is none.'''
        self.bump_cost(0.1)
        return self.env.donation_address

    async def phandle_banner(self) -> str:
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

    async def phandle_relayfee(self):
        """The minimum fee required for a transaction to be relayed on by the daemon to the
        bitcoin network. Doesn't guarantee mempool acceptance."""
        self.bump_cost(1.0)
        return await self.daemon_request('relayfee')

    async def phandle_mempool_info(self) -> dict[str, float]:
        """
        mempool.get_info, introduced in protocol 1.6.
        returns: {
            "mempoolminfee": BTC/kvB,
            "minrelaytxfee": BTC/kvB,
            "incrementalrelayfee": BTC/kvB,
        }
        """
        self.bump_cost(1.0)
        return await self.daemon_request('mempool_info')

    async def phandle_mempool_recent(self) -> list[dict[str, Any]]:
        """
        mempool.recent, introduced in protocol 1.6.1.
        Return a list of the last 10 transactions to enter the mempool.
        """
        self.bump_cost(1.0)
        recent_txs = await self.mempool.get_recently_added_txs(count=10)
        return [{
            "txid": hash_to_hex_str(tx.txid_rev),
            "fee": tx.fee,
            "vsize": tx.vsize,
        } for tx in recent_txs]

    async def phandle_estimatefee(self, number: int | Any, mode=None):
        '''The estimated transaction fee per kilobyte to be paid for a
        transaction to be included within a certain number of blocks.

        number: the number of blocks
        mode: CONSERVATIVE or ECONOMICAL estimation mode
        '''
        number = non_negative_integer(number)
        # use whitelist for mode, otherwise it would be easy to force a cache miss:
        mode = mode.upper() if isinstance(mode, str) else None
        if mode not in self.coin.ESTIMATEFEE_MODES:
            raise RPCError(BAD_REQUEST, f'unknown estimatefee mode: {mode}')
        self.bump_cost(0.1)

        number = self.coin.bucket_estimatefee_block_target(number)
        cache = self.session_mgr.estimatefee_cache

        cache_item = cache.get((number, mode))
        cache.num_lookups += 1
        if cache_item is not None:
            blockhash, feerate, lock = cache_item
            if blockhash and blockhash == self.session_mgr.bp.tip:
                cache.num_hits += 1
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
                    cache.num_hits += 1
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

    async def phandle_ping(self, pong_len=0, data=""):
        '''Serves as a connection keep-alive mechanism and for the client to
        confirm the server is still responding. It can also be used to obfuscate
        traffic patterns.
        '''
        self.bump_cost(0.1)
        if self.protocol_tuple < (1, 7):
            return None
        assert_hex_str(data, allow_odd_len=True)
        pong_len = non_negative_integer(pong_len)
        if pong_len > self.env.max_send:
            raise RPCError(BAD_REQUEST, f'pong_len value too high')
        pong_data = pong_len * "0"
        ret = {"data": pong_data}
        return ret

    async def phandle_on_ping_notification(self, data=""):
        self.bump_cost(0.1)  # note: the bw cost for receiving 'data' has already been incurred
        assert_hex_str(data, allow_odd_len=True)
        # nothing to do.
        # note: we could probabilistically send back a ping notif to the client, as noise,
        #       but we don't. Leave such logic to the client: if they wanted a response,
        #       they would have sent "server.ping" as a request instead of a notification.

    async def send_ping_notification_to_client(self, data_len: int) -> None:
        assert isinstance(data_len, int) and data_len >= 0, repr(data_len)
        data = "0" * data_len
        await self.send_notification("server.ping", (data,))

    async def phandle_server_version(
            self,
            client_name='',
            protocol_version=None,
            *extra_args,
            **extra_kwargs,
    ):
        '''Returns the server version as a string.

        client_name: a string identifying the client
        protocol_version: the protocol version spoken by the client

        note: extraneous unknown args for 'server.version' MUST be tolerated
              and ignored by the server, to allow for future extensions.
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

    async def phandle_transaction_broadcast(self, raw_tx: str | Any) -> str:
        '''Broadcast a raw transaction to the network.

        raw_tx: the raw transaction as a hexadecimal string'''
        assert_hex_str(raw_tx)
        self.bump_cost(0.25 + len(raw_tx) / 5000)
        # This returns errors as JSON RPC errors, as is natural
        try:
            txid_hum = await self.session_mgr.broadcast_transaction(raw_tx)
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
                    self.logger.info(f'sent tx: {txid_hum}. and warned user to upgrade their '
                                     f'client from {self.client}')
                    return msg

            self.logger.info(f'sent tx: {txid_hum}')
            return txid_hum

    async def phandle_package_broadcast(self, raw_txs: Sequence[str] | Any, verbose: bool = False) -> dict[str, Any]:
        """Broadcast a package of raw transactions to the network (submitpackage).
        The package must consist of a child with its parents,
        and none of the parents may depend on one another.

        raw_txs: a list of raw transactions as hexadecimal strings"""
        assert_list_or_tuple(raw_txs)
        for raw_tx in raw_txs:
            assert_hex_str(raw_tx)
        self.bump_cost(0.25 + sum(len(tx) / 5000 for tx in raw_txs))
        try:
            daemon_result = await self.session_mgr.broadcast_package(raw_txs)
        except DaemonError as e:
            error, = e.args
            message = error['message']
            self.logger.info(f"error submitting package: {message}")
            raise RPCError(
                BAD_REQUEST,
                f'the tx package was rejected by network rules.\n\n{message}.',
            )

        self.txs_sent += len(raw_txs)
        self.logger.info(f'broadcasted package: {len(raw_txs)=}')
        if verbose:
            return daemon_result

        response: dict[str, Union[bool, list]] = {
            'success': daemon_result['package_msg'] == 'success',
        }
        errors = []
        for tx in daemon_result.get('tx-results', {}).values():
            if tx.get('error'):
                error_msg = {
                    'txid': tx.get('txid'),
                    'error': tx['error']
                }
                errors.append(error_msg)
        if errors:
            response['errors'] = errors
        return response

    async def phandle_transaction_testmempoolaccept(self, raw_txs: Sequence[str]) -> Sequence[dict]:
        """Returns result of mempool acceptance tests indicating if txs would be accepted by mempool.

        raw_txs: a list of raw transactions as hexadecimal strings
        """
        assert_list_or_tuple(raw_txs)
        for raw_tx in raw_txs:
            assert_hex_str(raw_tx)
        self.bump_cost(0.25 + sum(len(tx) / 5000 for tx in raw_txs))
        daemon_result = await self.daemon_request("testmempoolaccept", raw_txs)

        response: list[dict] = []
        for orig_item in daemon_result:  # one item for each tx
            new_item = {
                "txid": orig_item["txid"],
                "wtxid": orig_item["wtxid"],
            }
            # optional: "allowed" field
            if orig_item.get("allowed") in (True, False):
                new_item["allowed"] = orig_item["allowed"]
            # optional: "reason" field
            reason_str = (
                    orig_item.get("package-error")
                    or orig_item.get("reject-details")
                    or orig_item.get("reject-reason")
                    or None)
            if reason_str is not None:
                new_item["reason"] = reason_str
            response.append(new_item)
        return response

    async def phandle_transaction_get(self, tx_hash: str | Any, verbose=False):
        '''Return the serialized raw transaction given its hash

        tx_hash: the transaction hash as a hexadecimal string
        verbose: passed on to the daemon
        '''
        assert_txid_hum(tx_hash)
        if verbose not in (True, False):
            raise RPCError(BAD_REQUEST, '"verbose" must be a boolean')

        self.bump_cost(1.0)
        return await self.daemon_request('getrawtransaction', tx_hash, verbose)

    async def phandle_transaction_merkle(self, tx_hash: str | Any, height: int | Any) -> dict[str, Any]:
        '''Return the merkle branch to a confirmed transaction given its hash
        and height.

        tx_hash: the transaction hash as a hexadecimal string
        height: the height of the block it is in
        '''
        txid_rev = assert_txid_hum(tx_hash)
        height = non_negative_integer(height)

        branch, tx_pos, block_header, cost = await self.session_mgr.merkle_branch_for_txid(
            txid_rev=txid_rev, height=height)
        self.bump_cost(cost)
        blockhash_hum = hash_to_hex_str(self.coin.header_hash_rev(block_header))

        return {
            "block_height": height,
            # "block_hash": blockhash_hum,
            "merkle": branch,
            "pos": tx_pos,
        }

    async def phandle_transaction_id_from_pos(self, height, tx_pos, merkle=False):
        '''Return the txid and optionally a merkle proof, given
        a block height and position in the block.
        '''
        tx_pos = non_negative_integer(tx_pos)
        height = non_negative_integer(height)
        if merkle not in (True, False):
            raise RPCError(BAD_REQUEST, '"merkle" must be a boolean')

        if merkle:
            branch, txid_hum, cost = await self.session_mgr.merkle_branch_for_tx_pos(
                height, tx_pos)
            self.bump_cost(cost)
            return {"tx_hash": txid_hum, "merkle": branch}
        else:
            txids_rev, cost = await self.session_mgr.txids_rev_at_blockheight(height)
            try:
                txid_rev = txids_rev[tx_pos]
            except IndexError:
                raise RPCError(BAD_REQUEST,
                               f'no tx at position {tx_pos:,d} in block at height {height:,d}')
            self.bump_cost(cost)
            txid_hum = hash_to_hex_str(txid_rev)
            return txid_hum

    async def phandle_compact_fee_histogram(self) -> Sequence[tuple[float, int]]:
        self.bump_cost(1.0)
        return await self.mempool.compact_fee_histogram()

    def set_request_handlers(self, ptuple):
        self.protocol_tuple = ptuple

        handlers = {
            'blockchain.block.header': self.phandle_block_header,
            'blockchain.block.headers': self.phandle_block_headers,
            'blockchain.estimatefee': self.phandle_estimatefee,
            'blockchain.headers.subscribe': self.phandle_headers_subscribe,
            'blockchain.transaction.broadcast': self.phandle_transaction_broadcast,
            'blockchain.transaction.get': self.phandle_transaction_get,
            'blockchain.transaction.get_merkle': self.phandle_transaction_merkle,
            'blockchain.transaction.id_from_pos': self.phandle_transaction_id_from_pos,
            'mempool.get_fee_histogram': self.phandle_compact_fee_histogram,
            'server.add_peer': self.phandle_add_peer,
            'server.banner': self.phandle_banner,
            'server.donation_address': self.phandle_donation_address,
            'server.features': self.phandle_server_features_async,
            'server.peers.subscribe': self.phandle_peers_subscribe,
            'server.ping': self.phandle_ping,
            'server.version': self.phandle_server_version,
        }
        notif_handlers = {}

        if ptuple < (1, 7):
            handlers['blockchain.scripthash.get_balance'] = self.phandle_scripthash_get_balance
            handlers['blockchain.scripthash.get_history'] = self.phandle_scripthash_get_history
            handlers['blockchain.scripthash.get_mempool'] = self.phandle_scripthash_get_mempool
            handlers['blockchain.scripthash.listunspent'] = self.phandle_scripthash_listunspent
            handlers['blockchain.scripthash.subscribe'] = self.phandle_scripthash_subscribe

        if (1, 4, 2) <= ptuple < (1, 7):
            handlers['blockchain.scripthash.unsubscribe'] = self.phandle_scripthash_unsubscribe

        if ptuple >= (1, 6):
            handlers['blockchain.transaction.broadcast_package'] = self.phandle_package_broadcast
            handlers['mempool.get_info'] = self.phandle_mempool_info
        else:
            handlers['blockchain.relayfee'] = self.phandle_relayfee  # removed in 1.6

        # experimental:
        if ptuple >= (1, 7):
            handlers['blockchain.transaction.testmempoolaccept'] = self.phandle_transaction_testmempoolaccept
            handlers['blockchain.outpoint.subscribe'] = self.phandle_txoutpoint_subscribe
            handlers['blockchain.outpoint.get_status'] = self.phandle_txoutpoint_get_status
            handlers['blockchain.outpoint.unsubscribe'] = self.phandle_txoutpoint_unsubscribe
            handlers['blockchain.scriptpubkey.get_balance'] = self.phandle_scriptpubkey_get_balance
            handlers['blockchain.scriptpubkey.get_history'] = self.phandle_scriptpubkey_get_history
            handlers['blockchain.scriptpubkey.get_mempool'] = self.phandle_scriptpubkey_get_mempool
            handlers['blockchain.scriptpubkey.listunspent'] = self.phandle_scriptpubkey_listunspent
            handlers['blockchain.scriptpubkey.subscribe'] = self.phandle_scriptpubkey_subscribe
            handlers['blockchain.scriptpubkey.unsubscribe'] = self.phandle_scriptpubkey_unsubscribe
            handlers['mempool.recent'] = self.phandle_mempool_recent
            notif_handlers['server.ping'] = self.phandle_on_ping_notification

        self.request_handlers = handlers
        self.notification_handlers = notif_handlers


class LocalRPC(SessionBase):
    '''A local TCP RPC server session.'''

    processing_timeout = 10**9  # disable timeouts

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.sv_seen = True
        self.sv_negotiated.set()
        self.client = 'RPC'
        self.connection.max_response_size = 0
        # note: self.request_handlers are set on the class, in SessionManager.__init__
        self.notification_handlers = {}

    def protocol_version_string(self):
        return 'RPC'


######################################################################
# Non-Bitcoin stuff goes strictly below this line.
######################################################################


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
            'masternode.announce.broadcast': self.phandle_masternode_announce_broadcast,
            'masternode.subscribe': self.phandle_masternode_subscribe,
            'masternode.list': self.phandle_masternode_list,
            'protx.diff': self.phandle_protx_diff,
            'protx.info': self.phandle_protx_info,
        })

    async def _notify_inner(
            self,
            *,
            touched_hashxs,
            touched_outpoints,
            height_changed,
    ):
        '''Notify the client about changes in masternode list.'''
        await super()._notify_inner(
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
    async def phandle_masternode_announce_broadcast(self, signmnb):
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

    async def phandle_masternode_subscribe(self, collateral):
        '''Returns the status of masternode.

        collateral: masternode collateral.
        '''
        result = await self.daemon_request('masternode_list',
                                           ('status', collateral))
        if result is not None:
            self.mns.add(collateral)
            return result.get(collateral)
        return None

    async def phandle_masternode_list(self, payees):
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

    async def phandle_protx_diff(self, base_height, height):
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

    async def phandle_protx_info(self, protx_hash):
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
            'smartrewards.current': self.phandle_smartrewards_current,
            'smartrewards.check': self.phandle_smartrewards_check
        })

    async def phandle_smartrewards_current(self):
        '''Returns the current smartrewards info.'''
        result = await self.daemon_request('smartrewards', ('current',))
        if result is not None:
            return result
        return None

    async def phandle_smartrewards_check(self, addr):
        '''
        Returns the status of an address

        addr: a single smartcash address
        '''
        result = await self.daemon_request('smartrewards', ('check', addr))
        if result is not None:
            return result
        return None


class AuxPoWElectrumX(ElectrumX):
    async def phandle_block_header(self, height, cp_height=0):
        result = await super().phandle_block_header(height, cp_height)

        # Older protocol versions don't truncate AuxPoW
        if self.protocol_tuple < (1, 4, 1):
            return result

        # Not covered by a checkpoint; return full AuxPoW data
        if cp_height == 0:
            return result

        # Covered by a checkpoint; truncate AuxPoW data
        result['header'] = self.truncate_auxpow_single(result['header'])
        return result

    async def phandle_block_headers(self, start_height, count, cp_height=0):
        # Older protocol versions don't truncate AuxPoW
        if self.protocol_tuple < (1, 4, 1):
            return await super().phandle_block_headers(start_height, count, cp_height)

        # Not covered by a checkpoint; return full AuxPoW data
        if cp_height == 0:
            return await super().phandle_block_headers(start_height, count, cp_height)

        result = await super().block_headers_array(start_height, count, cp_height)

        # Covered by a checkpoint; truncate AuxPoW data
        result['headers'] = self.truncate_auxpow_headers(result['headers'])

        # Return headers in array form
        if self.protocol_tuple >= (1, 6):
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


class NameIndexElectrumX(ElectrumX):
    def set_request_handlers(self, ptuple):
        super().set_request_handlers(ptuple)

        if ptuple >= (1, 4, 3):
            self.request_handlers['blockchain.name.get_value_proof'] = self.phandle_name_get_value_proof

    async def phandle_name_get_value_proof(self, scripthash, cp_height=0):
        history = await self.phandle_scripthash_get_history(scripthash)

        trimmed_history = []
        prev_height = None

        for update in history[::-1]:
            txid = update['tx_hash']
            height = update['height']

            if (self.coin.NAME_EXPIRATION is not None
                    and prev_height is not None
                    and height < prev_height - self.coin.NAME_EXPIRATION):
                break

            tx = await self.phandle_transaction_get(txid)
            update['tx'] = tx
            del update['tx_hash']

            tx_merkle = await self.phandle_transaction_merkle(txid, height)
            del tx_merkle['block_height']
            update['tx_merkle'] = tx_merkle

            if height <= cp_height:
                header = await self.phandle_block_header(height, cp_height)
                update['header'] = header

            trimmed_history.append(update)

            if height <= cp_height:
                break

            prev_height = height

        return {scripthash: trimmed_history}


class NameIndexAuxPoWElectrumX(NameIndexElectrumX, AuxPoWElectrumX):
    pass
