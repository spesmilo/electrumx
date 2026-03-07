# Copyright (c) 2016-2017, Neil Booth
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

'''Cryptograph hash functions and related classes.'''


import hashlib
import hmac

from electrumx.lib.util import bytes_to_int, int_to_bytes, hex_to_bytes

_sha256 = hashlib.sha256
_new_hash = hashlib.new
_hmac_digest = hmac.digest
HASHX_LEN = 11


def sha256(x):
    '''Simple wrapper of hashlib sha256.'''
    return _sha256(x).digest()


def double_sha256(x):
    '''SHA-256 of SHA-256, as used extensively in bitcoin.'''
    return sha256(sha256(x))


def hash_to_hex_str(x):
    '''Convert a big-endian binary hash to displayed hex string.

    Display form of a binary hash is reversed and converted to hex.
    '''
    return bytes(reversed(x)).hex()


def hex_str_to_hash(x):
    '''Convert a displayed hex string to a binary hash.'''
    return bytes(reversed(hex_to_bytes(x)))


class Base58Error(Exception):
    '''Exception used for Base58 errors.'''


class Base58:
    '''Class providing base 58 functionality.'''

    chars = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
    assert len(chars) == 58
    cmap = {c: n for n, c in enumerate(chars)}

    @staticmethod
    def char_value(c):
        val = Base58.cmap.get(c)
        if val is None:
            raise Base58Error(f'invalid base 58 character "{c}"')
        return val

    @staticmethod
    def decode(txt):
        """Decodes txt into a big-endian bytearray."""
        if not isinstance(txt, str):
            raise TypeError('a string is required')

        if not txt:
            raise Base58Error('string cannot be empty')

        value = 0
        for c in txt:
            value = value * 58 + Base58.char_value(c)

        result = int_to_bytes(value)

        # Prepend leading zero bytes if necessary
        count = 0
        for c in txt:
            if c != '1':
                break
            count += 1
        if count:
            result = bytes(count) + result

        return result

    @staticmethod
    def encode(be_bytes):
        """Converts a big-endian bytearray into a base58 string."""
        value = bytes_to_int(be_bytes)

        txt = ''
        while value:
            value, mod = divmod(value, 58)
            txt += Base58.chars[mod]

        for byte in be_bytes:
            if byte != 0:
                break
            txt += '1'

        return txt[::-1]

    @staticmethod
    def decode_check(txt, *, hash_fn=double_sha256):
        '''Decodes a Base58Check-encoded string to a payload.  The version
        prefixes it.'''
        be_bytes = Base58.decode(txt)
        result, check = be_bytes[:-4], be_bytes[-4:]
        if check != hash_fn(result)[:4]:
            raise Base58Error(f'invalid base 58 checksum for {txt}')
        return result

    @staticmethod
    def encode_check(payload, *, hash_fn=double_sha256):
        """Encodes a payload bytearray (which includes the version byte(s))
        into a Base58Check string."""
        be_bytes = payload + hash_fn(payload)[:4]
        return Base58.encode(be_bytes)


class Bech32Error(Exception):
    '''Exception used for Bech32 errors.'''


class Bech32:
    '''Class providing bech32 and bech32m functionality (BIP173/BIP350).'''

    CHARSET = 'qpzry9x8gf2tvdw0s3jn54khce6mua7l'
    BECH32_CONST = 1
    BECH32M_CONST = 0x2bc830a3

    @staticmethod
    def _polymod(values):
        '''Internal function that computes the bech32 checksum.'''
        GEN = [0x3b6a57b2, 0x26508e6d, 0x1ea119fa, 0x3d4233dd, 0x2a1462b3]
        chk = 1
        for v in values:
            b = chk >> 25
            chk = ((chk & 0x1ffffff) << 5) ^ v
            for i in range(5):
                chk ^= GEN[i] if ((b >> i) & 1) else 0
        return chk

    @staticmethod
    def _hrp_expand(hrp):
        '''Expand the HRP into values for checksum computation.'''
        return [ord(x) >> 5 for x in hrp] + [0] + [ord(x) & 31 for x in hrp]

    @classmethod
    def _verify_checksum(cls, hrp, data):
        '''Verify a checksum given HRP and converted data characters.
        Returns the encoding constant (BECH32_CONST or BECH32M_CONST) if valid.'''
        const = cls._polymod(cls._hrp_expand(hrp) + data)
        if const == cls.BECH32_CONST:
            return cls.BECH32_CONST
        if const == cls.BECH32M_CONST:
            return cls.BECH32M_CONST
        return None

    @classmethod
    def _create_checksum(cls, hrp, data, spec):
        '''Compute the checksum values given HRP, data and spec (BECH32 or BECH32M).'''
        const = cls.BECH32M_CONST if spec == 'bech32m' else cls.BECH32_CONST
        polymod = cls._polymod(cls._hrp_expand(hrp) + data + [0, 0, 0, 0, 0, 0]) ^ const
        return [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]

    @classmethod
    def _convertbits(cls, data, frombits, tobits, pad=True):
        '''General power-of-2 base conversion.'''
        acc = 0
        bits = 0
        ret = []
        maxv = (1 << tobits) - 1
        for value in data:
            if value < 0 or (value >> frombits):
                raise Bech32Error('invalid data value')
            acc = (acc << frombits) | value
            bits += frombits
            while bits >= tobits:
                bits -= tobits
                ret.append((acc >> bits) & maxv)
        if pad:
            if bits:
                ret.append((acc << (tobits - bits)) & maxv)
        elif bits >= frombits or ((acc << (tobits - bits)) & maxv):
            raise Bech32Error('invalid padding')
        return ret

    @classmethod
    def decode(cls, addr):
        '''Decode a bech32 or bech32m string.

        Returns (hrp, witness_version, witness_program) or raises Bech32Error.
        '''
        if any(ord(x) < 33 or ord(x) > 126 for x in addr):
            raise Bech32Error('invalid character')
        if addr.lower() != addr and addr.upper() != addr:
            raise Bech32Error('mixed case')
        addr = addr.lower()
        pos = addr.rfind('1')
        if pos < 1 or pos + 7 > len(addr) or len(addr) > 90:
            raise Bech32Error('invalid separator position')
        hrp = addr[:pos]
        data_part = addr[pos + 1:]

        # Decode data part
        try:
            data = [cls.CHARSET.index(c) for c in data_part]
        except ValueError:
            raise Bech32Error('invalid character in data part')

        # Verify checksum and get encoding type
        encoding = cls._verify_checksum(hrp, data)
        if encoding is None:
            raise Bech32Error('invalid checksum')

        # Extract witness version and program
        if len(data) < 7:
            raise Bech32Error('data too short')

        witness_version = data[0]
        if witness_version > 16:
            raise Bech32Error(f'invalid witness version: {witness_version}')

        # Convert from 5-bit to 8-bit
        try:
            witness_program = bytes(cls._convertbits(data[1:-6], 5, 8, pad=False))
        except Bech32Error:
            raise Bech32Error('invalid witness program padding')

        # Validate witness program length
        if len(witness_program) < 2 or len(witness_program) > 40:
            raise Bech32Error(f'invalid witness program length: {len(witness_program)}')

        # Witness version 0 must use bech32, version 1+ must use bech32m
        if witness_version == 0 and encoding != cls.BECH32_CONST:
            raise Bech32Error('witness v0 must use bech32 encoding')
        if witness_version != 0 and encoding != cls.BECH32M_CONST:
            raise Bech32Error('witness v1+ must use bech32m encoding')

        # Version-specific length requirements
        if witness_version == 0:
            if len(witness_program) != 20 and len(witness_program) != 32:
                raise Bech32Error('witness v0 program must be 20 or 32 bytes')
        elif witness_version == 1:
            if len(witness_program) != 32:
                raise Bech32Error('witness v1 (taproot) program must be 32 bytes')

        return hrp, witness_version, witness_program

    @classmethod
    def encode(cls, hrp, witness_version, witness_program):
        '''Encode a witness program to bech32 or bech32m.

        Uses bech32 for witness version 0, bech32m for version 1+.
        '''
        if witness_version < 0 or witness_version > 16:
            raise Bech32Error(f'invalid witness version: {witness_version}')
        if len(witness_program) < 2 or len(witness_program) > 40:
            raise Bech32Error(f'invalid witness program length: {len(witness_program)}')

        spec = 'bech32' if witness_version == 0 else 'bech32m'
        data = [witness_version] + cls._convertbits(witness_program, 8, 5)
        checksum = cls._create_checksum(hrp, data, spec)
        return hrp + '1' + ''.join(cls.CHARSET[d] for d in data + checksum)
