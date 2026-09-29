"""Zcash transaction decoding and wire-format validation."""

import pytest

from electrumx.lib.tx import DeserializerZcash, ZcashDeserializeError


def test_zcash_checked_read_boundaries():
    reader = DeserializerZcash(b'prefixabc', start=6)
    assert reader._zcash_read(0) == b''
    assert reader.cursor == 6
    assert reader._zcash_read(3) == b'abc'
    assert reader.cursor == 9
    assert reader._zcash_read(0) == b''
    with pytest.raises(ZcashDeserializeError, match='truncated'):
        reader._zcash_read(1)
    assert reader.cursor == 9


@pytest.mark.parametrize('size', [-1, 4, 2**64 - 1])
def test_zcash_checked_read_rejects_invalid_size(size):
    reader = DeserializerZcash(b'abc')
    with pytest.raises(ZcashDeserializeError, match='truncated'):
        reader._zcash_read(size)
    assert reader.cursor == 0


@pytest.mark.parametrize('encoded,value', [
    ('00', 0),
    ('fc', 252),
    ('fdfd00', 253),
    ('fdffff', 0xffff),
    ('fe00000100', 0x10000),
    ('feffffffff', 0xffffffff),
    ('ff0000000001000000', 0x100000000),
    ('ffffffffffffffffff', 0xffffffffffffffff),
])
def test_zcash_canonical_compact_size(encoded, value):
    raw = bytes.fromhex(encoded)
    reader = DeserializerZcash(raw + b'next')
    assert reader._zcash_varint() == value
    assert reader.cursor == len(raw)
    assert reader._zcash_read(4) == b'next'


@pytest.mark.parametrize('encoded', [
    'fd0000', 'fdfc00', 'feffff0000', 'ffffffffff00000000',
])
def test_zcash_rejects_noncanonical_compact_size(encoded):
    with pytest.raises(ZcashDeserializeError, match='non-canonical CompactSize'):
        DeserializerZcash(bytes.fromhex(encoded))._zcash_varint()


@pytest.mark.parametrize('encoded', ['', 'fd', 'fd01', 'fe000000', 'ff00000000000000'])
def test_zcash_rejects_truncated_compact_size(encoded):
    with pytest.raises(ZcashDeserializeError, match='truncated'):
        DeserializerZcash(bytes.fromhex(encoded))._zcash_varint()


@pytest.mark.parametrize('prefix,data', [(b'\x00', b''), (b'\x03', b'abc'),
                                       (b'\xfd\xfd\x00', b'x' * 253)])
def test_zcash_varbytes(prefix, data):
    reader = DeserializerZcash(prefix + data + b'next')
    assert reader._zcash_varbytes() == data
    assert reader.cursor == len(prefix) + len(data)
    assert reader._zcash_read(4) == b'next'


@pytest.mark.parametrize('raw', [b'\x03ab', b'\xff' * 9])
def test_zcash_rejects_truncated_varbytes(raw):
    with pytest.raises(ZcashDeserializeError, match='truncated'):
        DeserializerZcash(raw)._zcash_varbytes()
