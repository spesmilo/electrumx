"""Session bookkeeping with synthetic, non-networked clients."""

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from aiorpcx import NetAddress

from electrumx.server.session import SessionManager


@pytest.fixture
def manager():
    # Only initialize bookkeeping: no daemon, database, or listening sockets.
    manager = SessionManager.__new__(SessionManager)
    manager.env = SimpleNamespace(
        session_group_by_subnet_ipv4=24,
        session_group_by_subnet_ipv6=48,
    )
    manager.session_event = asyncio.Event()
    manager.sessions = {}
    manager.session_groups = {}
    return manager


@pytest.mark.parametrize('address, expected', [
    (None, 'unknown_addr'),
    (NetAddress('client.example', 50001), 'unknown_addr'),
    (NetAddress('8.8.8.8', 50001), '8.8.8.0/24'),
    (NetAddress('2606:4700:4700::1111', 50001), '2606:4700:4700::/48'),
    (NetAddress('127.0.0.1', 50001), None),
    (NetAddress('192.168.1.2', 50001), None),
    (NetAddress('::1', 50001), None),
    (NetAddress('fd00::1', 50001), None),
])
def test_session_address_group_lifecycle(manager, address, expected):
    session = Mock(cost=3.0)
    session.remote_address.return_value = address

    manager.add_session(session)

    session.remote_address.assert_called_once_with()
    assert manager.session_event.is_set()
    groups = manager.sessions[session]
    assert [group.name for group in groups] == ([] if expected is None else [expected])
    for group in groups:
        assert group.sessions == {session}
        assert group.weight == 1.0
        assert group.cost() == 3.0

    manager.session_event.clear()
    manager.remove_session(session)

    assert manager.session_event.is_set()
    assert manager.sessions == {}
    for group in groups:
        assert group.sessions == set()
        assert group.retained_cost == 3.0


def test_missing_addresses_share_unknown_group(manager):
    sessions = [Mock(cost=1.0), Mock(cost=2.0)]
    for session in sessions:
        session.remote_address.return_value = None
        manager.add_session(session)

    group = manager.session_groups['unknown_addr']
    assert group.sessions == set(sessions)
    assert group.cost() == 3.0
    for session in sessions:
        manager.remove_session(session)
    assert group.sessions == set()
    assert group.retained_cost == 3.0
