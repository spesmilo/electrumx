import pytest

from electrumx.server.controller import Notifications


@pytest.mark.asyncio
async def test_simple_mempool():
    n = Notifications()
    notified = []
    async def notify(*, touched_hashxs, touched_outpoints, height):
        notified.append((height, touched_hashxs))
    await n.start(5, notify)

    mtouched = {b'a', b'b'}
    btouched = {b'b', b'c'}
    await n.on_mempool(touched_hashxs=mtouched, height=6, touched_outpoints=set())
    assert notified == [(5, set())]
    await n.on_block(touched_hashxs=btouched, height=6, touched_outpoints=set())
    assert notified == [(5, set()), (6, set.union(mtouched, btouched))]


@pytest.mark.asyncio
async def test_enter_mempool_quick_blocks_2():
    n = Notifications()
    notified = []
    async def notify(*, touched_hashxs, touched_outpoints, height):
        notified.append((height, touched_hashxs))
    await n.start(5, notify)

    # Suppose a gets in block 6 and blocks 7,8 found right after and
    # the block processer processes them together.
    await n.on_mempool(touched_hashxs={b'a'}, height=5, touched_outpoints=set())
    assert notified == [(5, set()), (5, {b'a'})]
    # Mempool refreshes with daemon on block 6
    await n.on_mempool(touched_hashxs={b'a'}, height=6, touched_outpoints=set())
    assert notified == [(5, set()), (5, {b'a'})]
    # Blocks 6, 7 processed together
    await n.on_block(touched_hashxs={b'a', b'b'}, height=7, touched_outpoints=set())
    assert notified == [(5, set()), (5, {b'a'})]
    # Then block 8 processed
    await n.on_block(touched_hashxs={b'c'}, height=8, touched_outpoints=set())
    assert notified == [(5, set()), (5, {b'a'})]
    # Now mempool refreshes
    await n.on_mempool(touched_hashxs=set(), height=8, touched_outpoints=set())
    assert notified == [(5, set()), (5, {b'a'}), (8, {b'a', b'b', b'c'})]
