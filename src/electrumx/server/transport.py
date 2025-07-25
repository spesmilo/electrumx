# Copyright (c) 2025, The Electrum developers
#
# All rights reserved.
#
# See the file "LICENCE" for information about the copyright
# and warranty status of this software.

import asyncio
import time
from typing import Optional, TYPE_CHECKING

from aiorpcx.rawsocket import RSTransport

if TYPE_CHECKING:
    from .session import RPCSessionWithTaskGroup


class PaddedRSTransport(RSTransport):
    """A raw socket transport that provides basic countermeasures against traffic analysis
    by padding the jsonrpc payload with whitespaces to have ~uniform-size TCP packets.
    (it is assumed that a network observer does not see plaintext transport contents,
    due to it being wrapped e.g. in TLS)
    """

    MIN_PACKET_SIZE = 1024
    WAIT_FOR_BUFFER_GROWTH_SECONDS = 1.0

    session: Optional['RPCSessionWithTaskGroup']

    def __init__(self, *args, **kwargs):
        RSTransport.__init__(self, *args, **kwargs)
        self._sbuffer = bytearray()  # "send buffer"
        self._sbuffer_task = None  # type: Optional[asyncio.Task]
        self._sbuffer_has_data_evt = asyncio.Event()
        self._last_send = time.monotonic()
        self._force_send = False  # type: bool

    # note: this does not call super().write() but is a complete reimplementation
    async def write(self, message):
        await self._can_send.wait()
        if self.is_closing():
            return
        framed_message = self._framer.frame(message)
        self._sbuffer += framed_message
        self._sbuffer_has_data_evt.set()
        self._maybe_consume_sbuffer()

    def _maybe_consume_sbuffer(self) -> None:
        """Maybe take some data from sbuffer and send it on the wire."""
        if not self._can_send.is_set() or self.is_closing():
            return
        buf = self._sbuffer
        if not buf:
            return
        # if there is enough data in the buffer, or if we haven't sent in a while, send now:
        if not (
            self._force_send
            or len(buf) >= self.MIN_PACKET_SIZE
            or self._last_send + self.WAIT_FOR_BUFFER_GROWTH_SECONDS < time.monotonic()
        ):
            return
        assert buf[-2:] in (b"}\n", b"]\n"), f"unexpected json-rpc terminator: {buf[-2:]=!r}"
        # either (1) pad length to next power of two, to create "lsize" packet:
        payload_lsize = len(buf)
        total_lsize = max(self.MIN_PACKET_SIZE, 2 ** (payload_lsize.bit_length()))
        npad_lsize = total_lsize - payload_lsize
        # or if that wasted a lot of bandwidth with padding, (2) defer sending some messages
        # and create a packet with half that size ("ssize", s for small)
        total_ssize = max(self.MIN_PACKET_SIZE, total_lsize // 2)
        payload_ssize = buf.rfind(b"\n", 0, total_ssize)
        if payload_ssize != -1:
            payload_ssize += 1  # for "\n" char
            npad_ssize = total_ssize - payload_ssize
        else:
            npad_ssize = float("inf")
        # decide between (1) and (2):
        if self._force_send or npad_lsize <= npad_ssize:
            # (1) create "lsize" packet: consume full buffer
            npad = npad_lsize
            p_idx = payload_lsize
        else:
            # (2) create "ssize" packet: consume some, but defer some for later
            npad = npad_ssize
            p_idx = payload_ssize
        # pad by adding spaces near end
        json_rpc_terminator = buf[p_idx-2:p_idx]
        assert json_rpc_terminator in (b"}\n", b"]\n"), f"unexpected {json_rpc_terminator=!r}"
        buf2 = buf[:p_idx-2] + (npad * b" ") + json_rpc_terminator
        self._asyncio_transport.write(buf2)
        self._last_send = time.monotonic()
        del self._sbuffer[:p_idx]
        if not self._sbuffer:
            self._sbuffer_has_data_evt.clear()

    async def _poll_sbuffer(self):
        while not self.is_closing():
            await self._can_send.wait()
            await self._sbuffer_has_data_evt.wait()  # to avoid busy-waiting
            self._maybe_consume_sbuffer()
            # If there is still data in the buffer, sleep until it would time out.
            # note: If the transport is ~idle, when we wake up, we will send the current buf data,
            #       but if busy, we might wake up to completely new buffer contents. Either is fine.
            if len(self._sbuffer) > 0:
                timeout_abs = self._last_send + self.WAIT_FOR_BUFFER_GROWTH_SECONDS
                timeout_rel = max(0.0, timeout_abs - time.monotonic())
                await asyncio.sleep(timeout_rel)

    def connection_made(self, transport: asyncio.BaseTransport):
        super().connection_made(transport)
        coro = self.session.taskgroup.spawn(self._poll_sbuffer())
        self._sbuffer_task = self.loop.create_task(coro)
