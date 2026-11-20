import asyncio
import logging
from unittest.mock import AsyncMock

import pytest
from aiorpcx import ignore_after

from electrumx.lib.peer import Peer
from electrumx.server.peers import BadPeerError, PeerManager


class ControlledSession:
    def __init__(self, terminal_request, cancel_locally=False):
        self.terminal_request = terminal_request
        self.cancel_locally = cancel_locally
        self.closing = cancel_locally
        self.request_started = asyncio.Event()
        self.request_finished = asyncio.Event()

    def is_closing(self):
        return self.closing

    async def send_request(self, message, args=None):
        if message == 'server.version' and message != self.terminal_request:
            return ['ElectrumX', '1.4']
        if message != self.terminal_request:
            raise AssertionError(f'unexpected request: {message}')

        self.request_started.set()
        try:
            if self.cancel_locally:
                raise asyncio.CancelledError
            await asyncio.Future()
        finally:
            self.request_finished.set()


def make_peer_manager():
    manager = object.__new__(PeerManager)
    manager.logger = logging.getLogger('test.PeerManager')
    manager.server_version_args = ['ElectrumX', '1.4']
    manager._is_blacklisted = lambda peer: False
    manager._get_recent_good_peers = lambda: []
    manager._send_headers_subscribe = AsyncMock(return_value=None)
    manager._send_server_features = AsyncMock(return_value=None)
    manager._send_peers_subscribe = AsyncMock(return_value=[])
    manager._note_peers = AsyncMock(return_value=None)
    manager._features_to_register = lambda peer, peers: {'host': 'test'}
    return manager


def make_peer():
    host = 'test-peer.onion'
    return Peer(host, {'hosts': {host: {}}}, source='test')


async def verify_peer(session, expired_retry):
    if expired_retry:
        async with ignore_after(0):
            await asyncio.Future()
    await make_peer_manager()._verify_peer(session, make_peer())


@pytest.mark.parametrize('terminal_request', ['server.version', 'server.add_peer'])
@pytest.mark.parametrize('expired_retry', [False, True])
@pytest.mark.parametrize('session_closing', [False, True])
@pytest.mark.asyncio
async def test_verify_peer_propagates_task_cancellation(
        terminal_request, expired_retry, session_closing):
    session = ControlledSession(terminal_request)
    task = asyncio.create_task(verify_peer(session, expired_retry))

    await session.request_started.wait()
    session.closing = session_closing
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert session.request_finished.is_set()


@pytest.mark.parametrize('terminal_request', ['server.version', 'server.add_peer'])
@pytest.mark.parametrize('expired_retry', [False, True])
@pytest.mark.asyncio
async def test_verify_peer_marks_locally_cancelled_rpc_as_bad(
        terminal_request, expired_retry):
    session = ControlledSession(terminal_request, cancel_locally=True)
    task = asyncio.create_task(verify_peer(session, expired_retry))

    with pytest.raises(
            BadPeerError,
            match=f'terminated before {terminal_request} response'):
        await task
