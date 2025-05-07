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
    from .session import SessionBase


class PaddedRSTransport(RSTransport):
    """A raw socket transport that provides basic countermeasures against traffic analysis
    by padding the jsonrpc payload with whitespaces to have ~uniform-size TCP packets.
    (it is assumed that a network observer does not see plaintext transport contents,
    due to it being wrapped e.g. in TLS)
    """

    MIN_PAYLOAD_SIZE = 1024

    session: Optional['SessionBase']

    def __init__(self, *args, **kwargs):
        RSTransport.__init__(self, *args, **kwargs)
        self._sbuffer = bytearray()  # "send buffer"
        self._sbuffer_task = None  # type: Optional[asyncio.Task]
        self._sbuffer_has_data_evt = asyncio.Event()
        self._last_send = time.monotonic()

    async def write(self, message):
        await self._can_send.wait()
        if self.is_closing():
            return
        framed_message = self._framer.frame(message)
        self._sbuffer += framed_message
        self._sbuffer_has_data_evt.set()
        self._maybe_consume_sbuffer()
        if not self._sbuffer:
            self._sbuffer_has_data_evt.clear()

    def _maybe_consume_sbuffer(self):
        if not self._can_send.is_set() or self.is_closing():
            return
        buf = self._sbuffer
        if not buf:
            return
        # if there is enough data in the buffer, or if we haven't sent in a while, send now:
        if not (len(buf) >= self.MIN_PAYLOAD_SIZE or self._last_send + 1 < time.monotonic()):
            return
        assert buf[-2:] in (b"}\n", b"]\n"), f"unexpected json-rpc terminator: {buf[-2:]=!r}"
        # either (1) pad length to next power of two, to create "lsize" packet:
        payload_lsize = len(buf)
        total_lsize = max(self.MIN_PAYLOAD_SIZE, 2 ** (payload_lsize.bit_length()))
        npad_lsize = total_lsize - payload_lsize
        # or if that wasted a lot of bandwidth with padding, (2) defer sending some messages
        # and create a packet with half that size ("ssize", s for small)
        total_ssize = max(self.MIN_PAYLOAD_SIZE, total_lsize//2)
        payload_ssize = buf.rfind(b"\n", 0, total_ssize)
        if payload_ssize != -1:
            payload_ssize += 1  # for "\n" char
            npad_ssize = total_ssize - payload_ssize
        else:
            npad_ssize = float("inf")
        # decide between (1) and (2):
        if npad_lsize <= npad_ssize:
            npad = npad_lsize
            p_idx = payload_lsize
        else:
            npad = npad_ssize
            p_idx = payload_ssize
        # pad by adding spaces near end
        assert buf[p_idx-2:p_idx] in (b"}\n", b"]\n"), f"unexpected json-rpc terminator: {buf[p_idx-2:p_idx]=!r}"
        # self.session.maybe_log(
        #     f"PaddedRSTransport. calling low-level write(). "
        #     f"chose between (lsize:{payload_lsize}+{npad_lsize}, ssize:{payload_ssize}+{npad_ssize}). "
        #     f"won: {'tie' if npad_lsize == npad_ssize else 'lsize' if npad_lsize < npad_ssize else 'ssize'}."
        # )
        buf2 = buf[:p_idx - 2] + (npad * b" ") + buf[p_idx - 2:p_idx]
        self._asyncio_transport.write(buf2)
        self._last_send = time.monotonic()
        del self._sbuffer[:p_idx]

    async def _poll_sbuffer(self):
        while True:
            await asyncio.sleep(0.5)  # gives time for buffer to grow
            await self._sbuffer_has_data_evt.wait()  # lowers CPU cost compared to pure polling
            self._maybe_consume_sbuffer()

    def connection_made(self, transport: asyncio.BaseTransport):
        super().connection_made(transport)
        coro = self.session.taskgroup.spawn(self._poll_sbuffer())
        self._sbuffer_task = self.loop.create_task(coro)
