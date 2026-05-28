# Copyright (C) 2026 The Electrum developers
# Distributed under the MIT software license, see the accompanying
# file LICENCE or http://www.opensource.org/licenses/mit-license.php

from typing import Sequence

from electrumx.lib.util import (
    pack_le_uint32, unpack_le_uint32,
    pack_le_uint64, unpack_le_uint64,
    pack_be_uint32, unpack_be_uint32,
    pack_be_uint64, unpack_be_uint64,
)


# txnum
TXNUM_LEN = 5
TXNUM_PADDING = bytes(8 - TXNUM_LEN)


def unpack_txnum(tx_numb: bytes) -> int:
    return unpack_be_uint64(TXNUM_PADDING + tx_numb)[0]


def pack_txnum(tx_num: int) -> bytes:
    return pack_be_uint64(tx_num)[-TXNUM_LEN:]


# txout_idx
TXOUTIDX_LEN = 3
TXOUTIDX_PADDING = bytes(4 - TXOUTIDX_LEN)


def unpack_txoutidx(txout_idx: bytes) -> int:
    return unpack_le_uint32(txout_idx + TXOUTIDX_PADDING)[0]


def pack_txoutidx(txout_idx: int) -> bytes:
    return pack_le_uint32(txout_idx)[:TXOUTIDX_LEN]


# sats
def unpack_satoshis_val(sats: bytes) -> int:
    return unpack_le_uint64(sats)[0]


def pack_satoshis_val(sats: int) -> bytes:
    return pack_le_uint64(sats)


# block height
BHEIGHT_LEN = 4


def unpack_block_height(bheight: bytes) -> int:
    return unpack_be_uint32(bheight)[0]


def pack_block_height(bheight: int) -> bytes:
    return pack_be_uint32(bheight)


# dynamic block header offsets
DYN_HEADER_OFFSET_LEN = 8


def unpack_dyn_header_offset(offset: bytes) -> int:
    return unpack_le_uint64(offset)[0]


def pack_dyn_header_offset(offset: int) -> bytes:
    return pack_le_uint64(offset)


class DBTooOldForMigrations(RuntimeError):
    def __init__(self, *, db_name: str, db_version: int, supported_versions: Sequence[int]):
        cmd = 'rm -rf DB_DIRECTORY/{hist,meta,utxo}'
        super().__init__(
            f'Your {db_name} DB version is {db_version} but this software only handles versions {supported_versions}. '
            f'Manually delete your database (e.g. `{cmd}`, and start again. '
            f'Then, your DB will be rebuilt from genesis, likely taking several hours. '
            f"If you don't have time for this now, you can temporarily downgrade the software."
        )
