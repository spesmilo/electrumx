import asyncio
import logging
from unittest.mock import AsyncMock

import pytest

from electrumx.lib.peer import Peer
from electrumx.server.peers import BadPeerError, PeerManager


class ControlledSession:
    def __init__(self, terminal_request, cancel_locally=False):
        self.terminal_request = terminal_request
        self.cancel_locally = cancel_locally
        self.request_started = asyncio.Event()

    def is_closing(self):
        return self.cancel_locally

    async def send_request(self, message, args=None):
        if message == 'server.version' and message != self.terminal_request:
            return ['ElectrumX', '1.4']
        if message != self.terminal_request:
            raise AssertionError(f'unexpected request: {message}')

        self.request_started.set()
        if self.cancel_locally:
            # aiorpcX uses CancelledError for requests on an already-closed
            # session, independently of cancellation of the calling task.
            raise asyncio.CancelledError
        await asyncio.Future()


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


@pytest.mark.parametrize('terminal_request', ['server.version', 'server.add_peer'])
@pytest.mark.asyncio
async def test_verify_peer_propagates_task_cancellation(terminal_request):
    session = ControlledSession(terminal_request)
    task = asyncio.create_task(
        make_peer_manager()._verify_peer(session, make_peer()))

    await session.request_started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.parametrize('terminal_request', ['server.version', 'server.add_peer'])
@pytest.mark.asyncio
async def test_verify_peer_marks_locally_cancelled_rpc_as_bad(terminal_request):
    session = ControlledSession(terminal_request, cancel_locally=True)

    with pytest.raises(
            BadPeerError,
            match=f'terminated before {terminal_request} response'):
        await make_peer_manager()._verify_peer(session, make_peer())
