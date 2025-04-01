# Copyright (c) 2016-2017, Neil Booth
# Copyright (c) 2017, the ElectrumX authors
#
# All rights reserved.
#
# The MIT License (MIT)
#
# Permission is hereby granted, free of charge, to any person obtaining
# a copy of this software and associated documentation files (the
# "Software"), to deal in the Software without restriction, including
# without limitation the rights to use, copy, modify, merge, publish,
# distribute, sublicense, and/or sell copies of the Software, and to
# permit persons to whom the Software is furnished to do so, subject to
# the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE
# LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION
# WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
# and warranty status of this software.

'''Transaction-related classes and functions.'''

from dataclasses import dataclass
from hashlib import blake2s
from typing import Sequence, Optional, Tuple

from electrumx.lib.hash import sha256, double_sha256, hash_to_hex_str
from electrumx.lib.script import OpCodes
from electrumx.lib.util import (
    unpack_le_int32_from, unpack_le_int64_from, unpack_le_uint16_from,
    unpack_be_uint16_from,
    unpack_le_uint32_from, unpack_le_uint64_from, pack_le_int32, pack_varint,
    pack_le_uint16, pack_le_uint32, pack_le_int64, pack_varbytes,
)

ZERO = bytes(32)
MINUS_1 = 4294967295


class SkipTxDeserialize(Exception):
    '''Exception used to indicate transactions that should be skipped
    on account of certain deserialization issues.
    '''


# note: slotted dataclasses are a bit faster than namedtuples
@dataclass(kw_only=True, slots=True)
class Tx:
    '''Class representing a transaction.'''
    version: int
    inputs: Sequence['TxInput']
    outputs: Sequence['TxOutput']
    locktime: int
    # The hashes need to be reversed for human display;
    # for efficiency we process it in the natural serialized order.
    txid: bytes
    wtxid: bytes

    def serialize(self):
        return b''.join((
            pack_le_int32(self.version),
            pack_varint(len(self.inputs)),
            b''.join(tx_in.serialize() for tx_in in self.inputs),
            pack_varint(len(self.outputs)),
            b''.join(tx_out.serialize() for tx_out in self.outputs),
            pack_le_uint32(self.locktime)
        ))


@dataclass(kw_only=True, slots=True)
class TxInput:
    '''Class representing a transaction input.'''
    prev_hash: bytes
    prev_idx: int
    script: bytes
    sequence: int

    def __str__(self):
        script = self.script.hex()
        prev_hash = hash_to_hex_str(self.prev_hash)
        return (f"Input({prev_hash}, {self.prev_idx:d}, script={script}, "
                f"sequence={self.sequence:d})")

    def is_generation(self):
        '''Test if an input is generation/coinbase like'''
        return self.prev_idx == MINUS_1 and self.prev_hash == ZERO

    def serialize(self):
        return b''.join((
            self.prev_hash,
            pack_le_uint32(self.prev_idx),
            pack_varbytes(self.script),
            pack_le_uint32(self.sequence),
        ))


@dataclass(kw_only=True, slots=True)
class TxOutput:
    value: int
    pk_script: bytes

    def serialize(self):
        return b''.join((
            pack_le_int64(self.value),
            pack_varbytes(self.pk_script),
        ))


class Deserializer:
    '''Deserializes blocks into transactions.

    External entry points are read_tx(),
    read_tx_and_vsize() and read_block().

    This code is performance sensitive as it is executed 100s of
    millions of times during sync.
    '''

    TX_HASH_FN = staticmethod(double_sha256)

    def __init__(self, binary, start=0):
        assert isinstance(binary, bytes)
        self.binary = binary
        self.binary_length = len(binary)
        self.cursor = start

    def read_tx(self) -> Tx:
        '''Return a deserialized transaction.'''
        start = self.cursor
        tx = Tx(
            version=self._read_le_int32(),
            inputs=self._read_inputs(),
            outputs=self._read_outputs(),
            locktime=self._read_le_uint32(),
            txid=None,
            wtxid=None,
        )
        txid = self.TX_HASH_FN(self.binary[start:self.cursor])
        tx.txid = txid
        tx.wtxid = txid
        return tx

    def read_tx_and_vsize(self) -> Tuple[Tx, int]:
        '''Return a (deserialized TX, vsize) tuple.'''
        return self._read_tx_parts()

    def _read_tx_parts(self) -> Tuple[Tx, int]:
        '''Return a (deserialized TX, vsize) tuple.'''
        return self.read_tx(), self.binary_length

    def read_tx_block(self) -> Sequence[Tx]:
        read = self.read_tx
        # Some coins have excess data beyond the end of the transactions
        return [read() for _ in range(self._read_varint())]

    def _read_inputs(self):
        read_input = self._read_input
        return [read_input() for i in range(self._read_varint())]

    def _read_input(self):
        return TxInput(
            prev_hash=self._read_nbytes(32),
            prev_idx=self._read_le_uint32(),
            script=self._read_varbytes(),
            sequence=self._read_le_uint32(),
        )

    def _read_outputs(self):
        read_output = self._read_output
        return [read_output() for i in range(self._read_varint())]

    def _read_output(self):
        return TxOutput(
            value=self._read_le_int64(),
            pk_script=self._read_varbytes(),
        )

    def _read_byte(self):
        cursor = self.cursor
        self.cursor += 1
        return self.binary[cursor]

    def _read_nbytes(self, n):
        cursor = self.cursor
        self.cursor = end = cursor + n
        assert self.binary_length >= end
        return self.binary[cursor:end]

    def _read_varbytes(self):
        return self._read_nbytes(self._read_varint())

    def _read_varint(self):
        n = self.binary[self.cursor]
        self.cursor += 1
        if n < 253:
            return n
        if n == 253:
            return self._read_le_uint16()
        if n == 254:
            return self._read_le_uint32()
        return self._read_le_uint64()

    def _read_le_int32(self):
        result, = unpack_le_int32_from(self.binary, self.cursor)
        self.cursor += 4
        return result

    def _read_le_int64(self):
        result, = unpack_le_int64_from(self.binary, self.cursor)
        self.cursor += 8
        return result

    def _read_le_uint16(self):
        result, = unpack_le_uint16_from(self.binary, self.cursor)
        self.cursor += 2
        return result

    def _read_be_uint16(self):
        result, = unpack_be_uint16_from(self.binary, self.cursor)
        self.cursor += 2
        return result

    def _read_le_uint32(self):
        result, = unpack_le_uint32_from(self.binary, self.cursor)
        self.cursor += 4
        return result

    def _read_le_uint64(self):
        result, = unpack_le_uint64_from(self.binary, self.cursor)
        self.cursor += 8
        return result


@dataclass(kw_only=True, slots=True)
class TxSegWit(Tx):
    '''Class representing a SegWit transaction.'''
    marker: int
    flag: int
    witness: Sequence


class DeserializerSegWit(Deserializer):

    # https://bitcoincore.org/en/segwit_wallet_dev/#transaction-serialization

    def _read_witness(self, fields):
        read_witness_field = self._read_witness_field
        return [read_witness_field() for i in range(fields)]

    def _read_witness_field(self):
        read_varbytes = self._read_varbytes
        return [read_varbytes() for i in range(self._read_varint())]

    def _read_tx_parts(self) -> Tuple[Tx, int]:
        '''Return a (deserialized TX, vsize) tuple.'''
        start = self.cursor
        marker = self.binary[self.cursor + 4]
        if marker:  # non-segwit
            tx = Deserializer.read_tx(self)
            return tx, self.binary_length

        # Ugh, this is tasty.
        version = self._read_le_int32()
        orig_ser = self.binary[start:self.cursor]

        marker = self._read_byte()
        flag = self._read_byte()

        start = self.cursor
        inputs = self._read_inputs()
        outputs = self._read_outputs()
        orig_ser += self.binary[start:self.cursor]

        base_size = self.cursor - start
        witness = self._read_witness(len(inputs))

        start = self.cursor
        locktime = self._read_le_uint32()
        orig_ser += self.binary[start:self.cursor]
        vsize = (3 * base_size + self.binary_length) // 4

        txid = self.TX_HASH_FN(orig_ser)
        wtxid = self.TX_HASH_FN(self.binary[start:self.cursor])

        return TxSegWit(
            version=version,
            marker=marker,
            flag=flag,
            inputs=inputs,
            outputs=outputs,
            witness=witness,
            locktime=locktime,
            txid=txid,
            wtxid=wtxid), vsize

    def read_tx(self):
        return self._read_tx_parts()[0]


class DeserializerLitecoin(DeserializerSegWit):
    '''Class representing Litecoin transactions, which may have the MW flag set.

    This handles regular and segwit Litecoin transactions, with special handling
    for a limited set of MW transactions. All transactions in a block are parsed
    correctly without error, however certain MW transactions in mempool cannot
    be parsed and will raise a SkipTxDeserialize exception.

    When litecoind is run with the latest/default RPC serialization version (2),
    a SkipTxDeserialize exception will be raised for any transaction that has
    the MW flag bit set (0x8) AND has a non-null mwtx section.

    When litecoind is run with -rpcserialversion=1, only pure MW-only
    transactions that have no regular inputs or outputs will raise this
    exception. This is a workaround for a bug in v0.21.2.1 that lists these
    transactions in the getrawmempool result even though when served with the MW
    data stripped they are invalid. Presently, such transactions are of no
    interest to electrumx, and they vanish into the MWEB when mined.
    '''
    def _read_tx_parts(self):
        start = self.cursor
        marker = self.binary[self.cursor + 4]
        if marker:  # non-segwit
            tx = Deserializer.read_tx(self)
            return tx, self.binary_length

        version = self._read_le_int32()
        orig_ser = self.binary[start:self.cursor]

        marker = self._read_byte()
        flag = self._read_byte()

        # Work around a bug in litecoind v0.21.2 with -rpcserialversion=1 that
        # returns invalid stripped MW-only transactions with no regular inputs
        # or outputs, causing this to look like a segwit transaction, but it
        # really just has zeros for the number of inputs and outputs, which we
        # incorrectly interpreted as a marker and flag. If what we parsed as
        # marker and flag are both zero, this is such an invalid transaction.
        if flag == 0:
            raise SkipTxDeserialize('invalid MW-only transaction with no regular inputs or outputs')

        start = self.cursor
        inputs = self._read_inputs()
        outputs = self._read_outputs()
        orig_ser += self.binary[start:self.cursor]

        base_size = self.cursor - start

        # https://github.com/litecoin-project/litecoin/blob/948e6257aec15b52ef68b4e1ee9d73f7c740fae3/src/primitives/transaction.h#L299
        if flag & 1:  # witness flag
            witness = self._read_witness(len(inputs))
        else:
            # A MW tx is allowed to not have the witness bit of the flag byte
            # set, indicating no witness data encoded for the inputs. Perhaps we
            # should return a normal Tx here instead, but there is a flag byte
            # we should probably not just discard.
            witness = []

        if flag & 8:  # MWEB flag
            # If this transaction is in the main block, not the MW extension
            # block (MWEB), this should be the HogEx/integration transaction,
            # which has no mweb tx, just a single zero byte.
            # https://github.com/litecoin-project/litecoin/blob/bb242e33551157e0db3b70f90f5738f34b82cc51/src/mweb/mweb_node.cpp#L17-L20
            #
            # If this is anything but a zero byte, we cannot (yet) deserialize
            # this transaction since the mwtx at this location is variable
            # length and we cannot determine the locktime that follows without
            # accurately parsing it.
            #
            # Only in mempool should we encounter transactions with mwtx data,
            # so for the time being we will simply ignore these transactions.
            # When such transactions are mined, they will either have the MW
            # data stripped when they enter the regular block, or if they have
            # no regular inputs or outputs (MW-only) they will only be in the
            # MWEB, not the regular block at all.
            #
            # Note that by running litecoind with -rpcserialversion=1, the MW
            # data will be stripped from transactions and this tx flag will be
            # cleared.
            if self._read_byte() != 0:
                raise SkipTxDeserialize('non-null mwtx bytes are not parseable')

        start = self.cursor
        locktime = self._read_le_uint32()
        orig_ser += self.binary[start:self.cursor]
        vsize = (3 * base_size + self.binary_length) // 4

        txid = self.TX_HASH_FN(orig_ser)
        wtxid = self.TX_HASH_FN(self.binary[start:self.cursor])

        return TxSegWit(
            version=version,
            marker=marker,
            flag=flag,
            inputs=inputs,
            outputs=outputs,
            witness=witness,
            locktime=locktime,
            txid=txid,
            wtxid=wtxid), vsize

    def read_tx(self):
        return self._read_tx_parts()[0]


class DeserializerAuxPow(Deserializer):
    VERSION_AUXPOW = (1 << 8)

    def read_auxpow(self):
        '''Reads and returns the CAuxPow data'''

        # We first calculate the size of the CAuxPow instance and then
        # read it as bytes in the final step.
        start = self.cursor

        self.read_tx()  # AuxPow transaction
        self.cursor += 32  # Parent block hash
        merkle_size = self._read_varint()
        self.cursor += 32 * merkle_size  # Merkle branch
        self.cursor += 4  # Index
        merkle_size = self._read_varint()
        self.cursor += 32 * merkle_size  # Chain merkle branch
        self.cursor += 4  # Chain index
        self.cursor += 80  # Parent block header

        end = self.cursor
        self.cursor = start
        return self._read_nbytes(end - start)

    def read_header(self, static_header_size):
        '''Return the AuxPow block header bytes'''

        # We are going to calculate the block size then read it as bytes
        start = self.cursor

        version = self._read_le_uint32()
        if version & self.VERSION_AUXPOW:
            self.cursor = start
            self.cursor += static_header_size  # Block normal header
            self.read_auxpow()
            header_end = self.cursor
        else:
            header_end = start + static_header_size

        self.cursor = start
        return self._read_nbytes(header_end - start)


class DeserializerAuxPowSegWit(DeserializerSegWit, DeserializerAuxPow):
    pass


class DeserializerEquihash(Deserializer):
    def read_header(self, static_header_size):
        '''Return the block header bytes'''
        start = self.cursor
        # We are going to calculate the block size then read it as bytes
        self.cursor += static_header_size
        solution_size = self._read_varint()
        self.cursor += solution_size
        header_end = self.cursor
        self.cursor = start
        return self._read_nbytes(header_end)


class DeserializerEquihashSegWit(DeserializerSegWit, DeserializerEquihash):
    pass


class DeserializerZcash(DeserializerEquihash):
    def read_tx(self):
        start = self.cursor
        header = self._read_le_uint32()
        overwintered = ((header >> 31) == 1)
        if overwintered:
            version = header & 0x7fffffff
            self.cursor += 4  # versionGroupId
        else:
            version = header

        is_overwinter_v3 = version == 3
        is_sapling_v4 = version == 4

        base_tx = Tx(
            version=version,
            inputs=self._read_inputs(),
            outputs=self._read_outputs(),
            locktime=self._read_le_uint32(),
            txid=None,
            wtxid=None,
        )

        if is_overwinter_v3 or is_sapling_v4:
            self.cursor += 4  # expiryHeight

        has_shielded = False
        if is_sapling_v4:
            self.cursor += 8  # valueBalance
            shielded_spend_size = self._read_varint()
            self.cursor += shielded_spend_size * 384  # vShieldedSpend
            shielded_output_size = self._read_varint()
            self.cursor += shielded_output_size * 948  # vShieldedOutput
            has_shielded = shielded_spend_size > 0 or shielded_output_size > 0

        if base_tx.version >= 2:
            joinsplit_size = self._read_varint()
            if joinsplit_size > 0:
                joinsplit_desc_len = 1506 + (192 if is_sapling_v4 else 296)
                # JSDescription
                self.cursor += joinsplit_size * joinsplit_desc_len
                self.cursor += 32  # joinSplitPubKey
                self.cursor += 64  # joinSplitSig

        if is_sapling_v4 and has_shielded:
            self.cursor += 64  # bindingSig

        base_tx.txid = base_tx.wtxid = self.TX_HASH_FN(self.binary[start:self.cursor])
        return base_tx


@dataclass(kw_only=True, slots=True)
class TxPIVX(Tx):
    '''Class representing a PIVX transaction.'''
    txtype: int

    def serialize(self):
        return b''.join((
            pack_le_uint16(self.version),
            pack_le_uint16(self.txtype),
            pack_varint(len(self.inputs)),
            b''.join(tx_in.serialize() for tx_in in self.inputs),
            pack_varint(len(self.outputs)),
            b''.join(tx_out.serialize() for tx_out in self.outputs),
            pack_le_uint32(self.locktime)
        ))


class DeserializerPIVX(Deserializer):
    def read_tx(self):
        start = self.cursor
        header = self._read_le_uint32()
        tx_type = header >> 16  # DIP2 tx type
        if tx_type:
            version = header & 0x0000ffff
        else:
            version = header

        if tx_type and version < 3:
            version = header
            tx_type = 0

        base_tx = TxPIVX(
            version=version,
            txtype=tx_type,
            inputs=self._read_inputs(),
            outputs=self._read_outputs(),
            locktime=self._read_le_uint32(),
            txid=None,
            wtxid=None,
        )

        if version >= 3:  # >= sapling
            self._read_varint()
            self.cursor += 8  # valueBalance
            shielded_spend_size = self._read_varint()
            self.cursor += shielded_spend_size * 384  # vShieldedSpend
            shielded_output_size = self._read_varint()
            self.cursor += shielded_output_size * 948  # vShieldedOutput
            self.cursor += 64  # bindingSig
            if (tx_type > 0):
                self.cursor += 2  # extraPayload

        base_tx.txid = base_tx.wtxid = self.TX_HASH_FN(self.binary[start:self.cursor])
        return base_tx


@dataclass(kw_only=True, slots=True)
class TxTime(Tx):
    '''Class representing transaction that has a time field.'''
    time: int


class DeserializerTxTime(Deserializer):
    def read_tx(self):
        start = self.cursor
        tx = TxTime(
            version=self._read_le_int32(),
            time=self._read_le_uint32(),
            inputs=self._read_inputs(),
            outputs=self._read_outputs(),
            locktime=self._read_le_uint32(),
            txid=None,
            wtxid=None,
        )
        tx.txid = tx.wtxid = self.TX_HASH_FN(self.binary[start:self.cursor])
        return tx


@dataclass(kw_only=True, slots=True)
class TxTimeSegWit(TxSegWit):
    '''Class representing a SegWit transaction with time.'''
    time: int


class DeserializerTxTimeSegWit(DeserializerTxTime):
    def _read_witness(self, fields):
        read_witness_field = self._read_witness_field
        return [read_witness_field() for _ in range(fields)]

    def _read_witness_field(self):
        read_varbytes = self._read_varbytes
        return [read_varbytes() for _ in range(self._read_varint())]

    def _read_tx_parts(self):
        start = self.cursor
        marker = self.binary[self.cursor + 8]
        if marker:  # non-segwit
            tx = DeserializerTxTime.read_tx(self)
            return tx, self.binary_length

        version = self._read_le_int32()
        time = self._read_le_uint32()
        orig_ser = self.binary[start:self.cursor]

        marker = self._read_byte()
        flag = self._read_byte()

        start = self.cursor
        inputs = self._read_inputs()
        outputs = self._read_outputs()
        orig_ser += self.binary[start:self.cursor]

        base_size = self.cursor - start
        witness = self._read_witness(len(inputs))

        start = self.cursor
        locktime = self._read_le_uint32()
        orig_ser += self.binary[start:self.cursor]
        vsize = (3 * base_size + self.binary_length) // 4

        txid = self.TX_HASH_FN(orig_ser)
        wtxid = self.TX_HASH_FN(self.binary[start:self.cursor])

        tx = TxTimeSegWit(
            version=version,
            time=time,
            marker=marker,
            flag=flag,
            inputs=inputs,
            outputs=outputs,
            witness=witness,
            locktime=locktime,
            txid=txid,
            wtxid=wtxid,
        )
        return tx, vsize

    def read_tx(self):
        return self._read_tx_parts()[0]


class DeserializerTxTimeSegWitNavCoin(DeserializerTxTime):
    def _read_witness(self, fields):
        read_witness_field = self._read_witness_field
        return [read_witness_field() for _ in range(fields)]

    def _read_witness_field(self):
        read_varbytes = self._read_varbytes
        return [read_varbytes() for _ in range(self._read_varint())]

    def read_tx_no_segwit(self):
        start = self.cursor
        version = self._read_le_int32()
        time = self._read_le_uint32()
        inputs = self._read_inputs()
        outputs = self._read_outputs()
        locktime = self._read_le_uint32()
        strDZeel = ""
        if version >= 2:
            strDZeel = self._read_varbytes()
        txid = self.TX_HASH_FN(self.binary[start:self.cursor])
        return TxTime(
            version=version,
            time=time,
            inputs=inputs,
            outputs=outputs,
            locktime=locktime,
            txid=txid,
            wtxid=txid,
        )

    def _read_tx_parts(self):
        start = self.cursor
        marker = self.binary[self.cursor + 8]
        if marker:  # non-segwit
            tx = self.read_tx_no_segwit()
            return tx, self.binary_length

        version = self._read_le_int32()
        time = self._read_le_uint32()
        orig_ser = self.binary[start:self.cursor]

        marker = self._read_byte()
        flag = self._read_byte()

        start = self.cursor
        inputs = self._read_inputs()
        outputs = self._read_outputs()
        orig_ser += self.binary[start:self.cursor]

        base_size = self.cursor - start
        witness = self._read_witness(len(inputs))

        start = self.cursor
        locktime = self._read_le_uint32()
        strDZeel = ""

        if version >= 2:
            strDZeel = self._read_varbytes()

        vsize = (3 * base_size + self.binary_length) // 4
        orig_ser += self.binary[start:self.cursor]

        txid = self.TX_HASH_FN(orig_ser)
        wtxid = self.TX_HASH_FN(self.binary[start:self.cursor])
        tx = TxTimeSegWit(
            version=version,
            time=time,
            marker=marker,
            flag=flag,
            inputs=inputs,
            outputs=outputs,
            witness=witness,
            locktime=locktime,
            txid=txid,
            wtxid=wtxid,
        )
        return tx, vsize

    def read_tx(self):
        return self._read_tx_parts()[0]


@dataclass(kw_only=True, slots=True)
class TxTrezarcoin(Tx):
    '''Class representing transaction that has a time and txcomment field.'''
    time: int
    txcomment: bytes


class DeserializerTrezarcoin(Deserializer):

    def read_tx(self):
        start = self.cursor
        version = self._read_le_int32()
        time = self._read_le_uint32()
        inputs = self._read_inputs()
        outputs = self._read_outputs()
        locktime = self._read_le_uint32()
        if version >= 2:
            txcomment = self._read_varbytes()
        else:
            txcomment = b''
        txid = self.TX_HASH_FN(self.binary[start:self.cursor])
        return TxTrezarcoin(
            version=version,
            time=time,
            inputs=inputs,
            outputs=outputs,
            locktime=locktime,
            txcomment=txcomment,
            txid=txid,
            wtxid=txid,
        )

    @staticmethod
    def blake2s_gen(data):
        keyOne = data[36:46]
        keyTwo = data[58:68]
        ntime = data[68:72]
        _nBits = data[72:76]
        _nonce = data[76:80]
        _full_merkle = data[36:68]
        _input112 = data + _full_merkle
        _key = keyTwo + ntime + _nBits + _nonce + keyOne
        # Prepare 112Byte Header
        blake2s_hash = blake2s(_input112, digest_size=32, key=_key)
        # TrezarFlips - Only for Genesis
        return ''.join(map(str.__add__, blake2s_hash.hexdigest()[-2::-2],
                           blake2s_hash.hexdigest()[-1::-2]))

    @staticmethod
    def blake2s(data):
        keyOne = data[36:46]
        keyTwo = data[58:68]
        ntime = data[68:72]
        _nBits = data[72:76]
        _nonce = data[76:80]
        _full_merkle = data[36:68]
        _input112 = data + _full_merkle
        _key = keyTwo + ntime + _nBits + _nonce + keyOne
        # Prepare 112Byte Header
        blake2s_hash = blake2s(_input112, digest_size=32, key=_key)
        # TrezarFlips
        return blake2s_hash.digest()


class DeserializerBlackcoin(Deserializer):
    BLACKCOIN_TX_VERSION = 2

    def _get_version(self):
        result, = unpack_le_int32_from(self.binary, self.cursor)
        return result

    def read_tx(self):
        start = self.cursor
        version = self._get_version()
        if version < self.BLACKCOIN_TX_VERSION:
            tx = TxTime(
                version=self._read_le_int32(),
                time=self._read_le_uint32(),
                inputs=self._read_inputs(),
                outputs=self._read_outputs(),
                locktime=self._read_le_uint32(),
                txid=None,
                wtxid=None,
            )
        else:
            tx = Tx(
                version=self._read_le_int32(),
                inputs=self._read_inputs(),
                outputs=self._read_outputs(),
                locktime=self._read_le_uint32(),
                txid=None,
                wtxid=None,
            )
        tx.txid = tx.wtxid = self.TX_HASH_FN(self.binary[start:self.cursor])
        return tx


class DeserializerReddcoin(Deserializer):
    def read_tx(self):
        start = self.cursor
        version = self._read_le_int32()
        inputs = self._read_inputs()
        outputs = self._read_outputs()
        locktime = self._read_le_uint32()
        if version > 1:
            time = self._read_le_uint32()
        else:
            time = 0

        txid = self.TX_HASH_FN(self.binary[start:self.cursor])
        return TxTime(
            version=version,
            time=time,
            inputs=inputs,
            outputs=outputs,
            locktime=locktime,
            txid=txid,
            wtxid=txid,
        )


class DeserializerVerge(Deserializer):
    def read_tx(self):
        start = self.cursor
        version = self._read_le_int32()
        time = self._read_le_uint32()
        inputs = self._read_inputs()
        outputs = self._read_outputs()
        locktime = self._read_le_uint32()

        txid = self.TX_HASH_FN(self.binary[start:self.cursor])
        return TxTime(
            version=version,
            time=time,
            inputs=inputs,
            outputs=outputs,
            locktime=locktime,
            txid=txid,
            wtxid=txid,
        )


class DeserializerEmercoin(DeserializerTxTimeSegWit):
    VERSION_AUXPOW = (1 << 8)

    def is_merged_block(self):
        start = self.cursor
        self.cursor = 0
        version = self._read_le_uint32()
        self.cursor = start
        if version & self.VERSION_AUXPOW:
            return True
        return False

    def read_header(self, static_header_size):
        '''Return the AuxPow block header bytes'''
        start = self.cursor
        version = self._read_le_uint32()
        if version & self.VERSION_AUXPOW:
            # We are going to calculate the block size then read it as bytes
            self.cursor = start
            self.cursor += static_header_size  # Block normal header
            self.read_tx()  # AuxPow transaction
            self.cursor += 32  # Parent block hash
            merkle_size = self._read_varint()
            self.cursor += 32 * merkle_size  # Merkle branch
            self.cursor += 4  # Index
            merkle_size = self._read_varint()
            self.cursor += 32 * merkle_size  # Chain merkle branch
            self.cursor += 4  # Chain index
            self.cursor += 80  # Parent block header
            header_end = self.cursor
        else:
            header_end = static_header_size
        self.cursor = start
        return self._read_nbytes(header_end)


class DeserializerBitcoinAtom(DeserializerSegWit):
    FORK_BLOCK_HEIGHT = 505888

    def read_header(self, height, static_header_size):
        '''Return the block header bytes'''
        header_len = static_header_size
        if height >= self.FORK_BLOCK_HEIGHT:
            header_len += 4  # flags
        return self._read_nbytes(header_len)


class DeserializerGroestlcoin(DeserializerSegWit):
    TX_HASH_FN = staticmethod(sha256)


class TxInputTokenPay(TxInput):
    '''Class representing a TokenPay transaction input.'''

    OP_ANON_MARKER = 0xb9
    # 2byte marker (cpubkey + sigc + sigr)
    MIN_ANON_IN_SIZE = 2 + (33 + 32 + 32)

    def _is_anon_input(self):
        return (len(self.script) >= self.MIN_ANON_IN_SIZE and
                self.script[0] == OpCodes.OP_RETURN and
                self.script[1] == self.OP_ANON_MARKER)

    def is_generation(self):
        # Transactions coming in from stealth addresses are seen by
        # the blockchain as newly minted coins. The reverse, where coins
        # are sent TO a stealth address, are seen by the blockchain as
        # a coin burn.
        if self._is_anon_input():
            return True
        return super(TxInputTokenPay, self).is_generation()


@dataclass(kw_only=True, slots=True)
class TxInputTokenPayStealth:
    '''Class representing a TokenPay stealth transaction input.'''
    keyimage: bytes
    ringsize: bytes
    script: bytes
    sequence: int

    def __str__(self):
        script = self.script.hex()
        keyimage = bytes(self.keyimage).hex()
        return (f"Input({keyimage}, {self.ringsize[1]:d}, script={script}, "
                f"sequence={self.sequence:d})")

    def is_generation(self):
        return True

    def serialize(self):
        return b''.join((
            self.keyimage,
            self.ringsize,
            pack_varbytes(self.script),
            pack_le_uint32(self.sequence),
        ))


class DeserializerTokenPay(DeserializerTxTime):

    def _read_input(self):
        txin = TxInputTokenPay(
            prev_hash=self._read_nbytes(32),
            prev_idx=self._read_le_uint32(),
            script=self._read_varbytes(),
            sequence=self._read_le_uint32(),
        )
        if txin._is_anon_input():
            # Not sure if this is actually needed, and seems
            # extra work for no immediate benefit, but it at
            # least correctly represents a stealth input
            raw = txin.serialize()
            deserializer = Deserializer(raw)
            txin = TxInputTokenPayStealth(
                keyimage=deserializer._read_nbytes(33),
                ringsize=deserializer._read_nbytes(3),
                script=deserializer._read_varbytes(),
                sequence=deserializer._read_le_uint32(),
            )
        return txin


# Decred
@dataclass(kw_only=True, slots=True)
class TxInputDcr:
    '''Class representing a Decred transaction input.'''
    prev_hash: bytes
    prev_idx: int
    tree: int
    sequence: int

    def __str__(self):
        prev_hash = hash_to_hex_str(self.prev_hash)
        return (f"Input({prev_hash}, {self.prev_idx:d}, tree={self.tree}, "
                f"sequence={self.sequence:d})")

    def is_generation(self):
        '''Test if an input is generation/coinbase like'''
        return self.prev_idx == MINUS_1 and self.prev_hash == ZERO


@dataclass(kw_only=True, slots=True)
class TxOutputDcr:
    '''Class representing a Decred transaction output.'''
    value: int
    version: int
    pk_script: bytes


@dataclass(kw_only=True, slots=True)
class TxDcr(Tx):
    '''Class representing a Decred  transaction.'''
    expiry: int
    witness: Sequence


class DeserializerDecred(Deserializer):
    @staticmethod
    def blake256(data):
        from blake256.blake256 import blake_hash
        return blake_hash(data)

    @staticmethod
    def blake256d(data):
        from blake256.blake256 import blake_hash
        return blake_hash(blake_hash(data))

    def read_tx(self):
        return self._read_tx_parts()[0]

    def read_tx_block(self):
        read = self.read_tx
        txs = [read() for _ in range(self._read_varint())]
        stxs = [read() for _ in range(self._read_varint())]
        return txs + stxs

    def read_tx_tree(self):
        '''Returns a list of deserialized_tx without tx hashes.'''
        read_tx = self.read_tx
        return [read_tx() for _ in range(self._read_varint())]

    def _read_input(self):
        return TxInputDcr(
            prev_hash=self._read_nbytes(32),
            prev_idx=self._read_le_uint32(),
            tree=self._read_byte(),
            sequence=self._read_le_uint32(),
        )

    def _read_output(self):
        return TxOutputDcr(
            value=self._read_le_int64(),
            version=self._read_le_uint16(),
            pk_script=self._read_varbytes(),
        )

    def _read_witness(self, fields):
        read_witness_field = self._read_witness_field
        assert fields == self._read_varint()
        return [read_witness_field() for _ in range(fields)]

    def _read_witness_field(self):
        value_in = self._read_le_int64()
        block_height = self._read_le_uint32()
        block_index = self._read_le_uint32()
        script = self._read_varbytes()
        return value_in, block_height, block_index, script

    def _read_tx_parts(self):
        start = self.cursor
        version = self._read_le_int32()
        inputs = self._read_inputs()
        outputs = self._read_outputs()
        locktime = self._read_le_uint32()
        expiry = self._read_le_uint32()
        end_prefix = self.cursor
        witness = self._read_witness(len(inputs))

        # TxSerializeNoWitness << 16 == 0x10000
        no_witness_header = pack_le_uint32(0x10000 | (version & 0xffff))
        prefix_tx = no_witness_header + self.binary[start+4:end_prefix]
        tx_hash = self.blake256(prefix_tx)

        return TxDcr(
            version=version,
            inputs=inputs,
            outputs=outputs,
            locktime=locktime,
            expiry=expiry,
            witness=witness,
            txid=tx_hash,
            wtxid=tx_hash,
        ), self.cursor - start


class DeserializerSmartCash(Deserializer):
    TX_HASH_FN = staticmethod(sha256)

    @staticmethod
    def keccak(data):
        from Cryptodome.Hash import keccak
        keccak_hash = keccak.new(data=data, digest_bits=256)
        return keccak_hash.digest()


@dataclass(kw_only=True, slots=True)
class TxBitcoinDiamond(Tx):
    '''Class representing a transaction.'''
    preblockhash: str


class DeserializerBitcoinDiamond(Deserializer):
    bitcoin_diamond_tx_version = 12

    def read_tx(self):
        # Return a Deserialized TX.
        start = self.cursor
        version = self._get_version()
        if version != self.bitcoin_diamond_tx_version:
            tx = Tx(
                version=self._read_le_int32(),
                inputs=self._read_inputs(),
                outputs=self._read_outputs(),
                locktime=self._read_le_uint32(),
                txid=None,
                wtxid=None,
            )
        else:
            tx = TxBitcoinDiamond(
                version=self._read_le_int32(),
                preblockhash=hash_to_hex_str(self._read_nbytes(32)),
                inputs=self._read_inputs(),
                outputs=self._read_outputs(),
                locktime=self._read_le_uint32(),
                txid=None,
                wtxid=None,
            )
        txid = self.TX_HASH_FN(self.binary[start:self.cursor])
        tx.txid = tx.wtxid = txid
        return tx

    def _get_version(self):
        result, = unpack_le_int32_from(self.binary, self.cursor)
        return result


@dataclass(kw_only=True, slots=True)
class TxBitcoinDiamondSegWit(TxSegWit):
    '''Class representing a SegWit transaction.'''
    preblockhash: str


class DeserializerBitcoinDiamondSegWit(DeserializerBitcoinDiamond,
                                       DeserializerSegWit):
    def _read_tx_parts(self):
        start = self.cursor
        tx_version = self._get_version()
        if tx_version == self.bitcoin_diamond_tx_version:
            marker = self.binary[self.cursor + 4 + 32]
        else:
            marker = self.binary[self.cursor + 4]

        if marker:  # non-segwit
            tx = DeserializerBitcoinDiamond.read_tx(self)
            return tx, self.binary_length

        # Ugh, this is nasty.
        version = self._read_le_int32()
        present_block_hash = None
        if version == self.bitcoin_diamond_tx_version:
            present_block_hash = hash_to_hex_str(self._read_nbytes(32))
        orig_ser = self.binary[start:self.cursor]

        marker = self._read_byte()
        flag = self._read_byte()

        start = self.cursor
        inputs = self._read_inputs()
        outputs = self._read_outputs()
        orig_ser += self.binary[start:self.cursor]

        base_size = self.cursor - start
        witness = self._read_witness(len(inputs))

        start = self.cursor
        locktime = self._read_le_uint32()
        orig_ser += self.binary[start:self.cursor]
        vsize = (3 * base_size + self.binary_length) // 4

        txid = self.TX_HASH_FN(orig_ser)
        wtxid = self.TX_HASH_FN(self.binary[start:self.cursor])

        if present_block_hash is not None:
            return TxBitcoinDiamondSegWit(
                version=version,
                preblockhash=present_block_hash,
                marker=marker,
                flag=flag,
                inputs=inputs,
                outputs=outputs,
                witness=witness,
                locktime=locktime,
                txid=txid,
                wtxid=wtxid), vsize
        else:
            return TxSegWit(
                version=version,
                marker=marker,
                flag=flag,
                inputs=inputs,
                outputs=outputs,
                witness=witness,
                locktime=locktime,
                txid=txid,
                wtxid=wtxid), vsize

    def read_tx(self):
        '''Return a (Deserialized TX, TX_HASH) pair.

        The hash needs to be reversed for human display; for efficiency
        we process it in the natural serialized order.
        '''
        return self._read_tx_parts()[0]


class DeserializerElectra(Deserializer):
    ELECTRA_TX_VERSION = 7

    def _get_version(self):
        result, = unpack_le_int32_from(self.binary, self.cursor)
        return result

    def read_tx(self):
        start = self.cursor
        version = self._get_version()
        if version != self.ELECTRA_TX_VERSION:
            tx = TxTime(
                version=self._read_le_int32(),
                time=self._read_le_uint32(),
                inputs=self._read_inputs(),
                outputs=self._read_outputs(),
                locktime=self._read_le_uint32(),
                txid=None,
                wtxid=None,
            )
        else:
            tx = Tx(
                version=self._read_le_int32(),
                inputs=self._read_inputs(),
                outputs=self._read_outputs(),
                locktime=self._read_le_uint32(),
                txid=None,
                wtxid=None,
            )
        tx.txid = tx.wtxid = self.TX_HASH_FN(self.binary[start:self.cursor])
        return tx


class DeserializerECCoin(Deserializer):
    def read_tx(self):
        start = self.cursor
        tx_version = self._read_le_int32()
        tx = TxTime(
            version=tx_version,
            time=self._read_le_uint32(),
            inputs=self._read_inputs(),
            outputs=self._read_outputs(),
            locktime=self._read_le_uint32(),
            txid=None,
            wtxid=None,
        )

        if tx_version > 1:
            self.cursor += 32

        tx.txid = tx.wtxid = self.TX_HASH_FN(self.binary[start:self.cursor])
        return tx


class DeserializerZcoin(Deserializer):
    def _read_input(self):
        tx_input = TxInput(
            prev_hash=self._read_nbytes(32),
            prev_idx=self._read_le_uint32(),
            script=self._read_varbytes(),
            sequence=self._read_le_uint32(),
        )

        if tx_input.prev_idx == MINUS_1 and tx_input.prev_hash == ZERO:
            return tx_input

        if tx_input.script[0] == 0xc4:  # This is a Sigma spend - mimic a generation tx
            return TxInput(
                prev_hash=ZERO,
                prev_idx=MINUS_1,
                script=tx_input.script,
                sequence=tx_input.sequence
            )

        return tx_input


class DeserializerXaya(DeserializerSegWit, DeserializerAuxPow):
    """Deserializer class for the Xaya network

    The main difference to other networks is the changed format of the
    block header with "triple purpose mining", see
    https://github.com/xaya/xaya/blob/master/doc/xaya/mining.md.

    This builds upon classic auxpow, but has a modified serialisation format
    that we have to implement here."""

    MM_FLAG = 0x80

    def read_header(self, static_header_size):
        """Reads in the full block header (including PoW data)"""

        # We first calculate the dynamic size of the block header, and then
        # read in all the data in the final step.
        start = self.cursor

        self.cursor += static_header_size  # Normal block header

        algo = self._read_byte()
        self._read_le_uint32()  # nBits

        if algo & self.MM_FLAG:
            self.read_auxpow()
        else:
            self.cursor += static_header_size  # Fake header

        end = self.cursor
        self.cursor = start
        return self._read_nbytes(end - start)


class DeserializerSimplicity(Deserializer):
    SIMPLICITY_TX_VERSION = 3

    def _get_version(self):
        result, = unpack_le_int32_from(self.binary, self.cursor)
        return result

    def read_tx(self):
        start = self.cursor
        version = self._get_version()
        if version < self.SIMPLICITY_TX_VERSION:
            tx = TxTime(
                version=self._read_le_int32(),
                time=self._read_le_uint32(),
                inputs=self._read_inputs(),
                outputs=self._read_outputs(),
                locktime=self._read_le_uint32(),
                txid=None,
                wtxid=None,
            )
        else:
            tx = Tx(
                version=self._read_le_int32(),
                inputs=self._read_inputs(),
                outputs=self._read_outputs(),
                locktime=self._read_le_uint32(),
                txid=None,
                wtxid=None,
            )
        tx.txid = tx.wtxid = self.TX_HASH_FN(self.binary[start:self.cursor])
        return tx


class DeserializerPrimecoin(Deserializer):
    def read_header(self, static_header_size):
        '''Return the block header bytes'''
        start = self.cursor
        # Decode the block header size including multiplier then read it as bytes
        self.cursor += static_header_size
        multiplier_size = self._read_varint()
        self.cursor += multiplier_size
        header_end = self.cursor
        self.cursor = start
        return self._read_nbytes(header_end - start)
