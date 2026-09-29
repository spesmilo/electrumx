"""Zcash transaction decoding and wire-format validation."""

from hashlib import blake2b

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


# One input and one output; only the input script is outside the txid digest.
TRANSPARENT_BUNDLE = bytes.fromhex(
    '01' + '11' * 32 + '07000000' + '03736967' + '04030201' +
    '01' + '3930000000000000' + '0151')


def test_zcash_transparent_bundle_fields_and_digest():
    reader = DeserializerZcash(TRANSPARENT_BUNDLE + b'next')
    inputs, outputs, scripts, digest = reader._zcash_transparent()
    assert len(inputs) == len(outputs) == 1
    assert inputs[0].prev_txid_rev == b'\x11' * 32
    assert inputs[0].prev_idx == 7
    assert inputs[0].script == b'sig'
    assert inputs[0].sequence == 0x01020304
    assert outputs[0].value == 12345
    assert outputs[0].pk_script == b'\x51'
    assert scripts == [b'\x03sig']
    assert reader.cursor == len(TRANSPARENT_BUNDLE)
    assert reader._zcash_read(4) == b'next'

    # Independent construction from the known fields, not the parser's slices.
    prevouts = blake2b(b'\x11' * 32 + bytes.fromhex('07000000'),
                      digest_size=32, person=b'ZTxIdPrevoutHash').digest()
    sequences = blake2b(bytes.fromhex('04030201'), digest_size=32,
                       person=b'ZTxIdSequencHash').digest()
    tx_outputs = blake2b(bytes.fromhex('39300000000000000151'), digest_size=32,
                        person=b'ZTxIdOutputsHash').digest()
    expected = blake2b(prevouts + sequences + tx_outputs, digest_size=32,
                       person=b'ZTxIdTranspaHash').digest()
    assert digest == expected


def test_zcash_empty_transparent_bundle():
    reader = DeserializerZcash(b'\x00\x00next')
    inputs, outputs, scripts, digest = reader._zcash_transparent()
    assert inputs == outputs == scripts == []
    assert digest == blake2b(b'', digest_size=32, person=b'ZTxIdTranspaHash').digest()
    assert reader.cursor == 2


@pytest.mark.parametrize('raw,input_count,output_count', [
    (TRANSPARENT_BUNDLE[:45] + b'\x00', 1, 0),
    (b'\x00' + TRANSPARENT_BUNDLE[45:], 0, 1),
])
def test_zcash_one_sided_transparent_bundle(raw, input_count, output_count):
    reader = DeserializerZcash(raw)
    inputs, outputs, scripts, digest = reader._zcash_transparent()
    assert len(inputs) == len(scripts) == input_count
    assert len(outputs) == output_count
    assert reader.cursor == len(raw)
    assert digest != blake2b(b'', digest_size=32, person=b'ZTxIdTranspaHash').digest()


def test_zcash_transparent_digest_excludes_scriptsig():
    # Replace both the script and its CompactSize prefix, changing its length.
    changed = TRANSPARENT_BUNDLE[:37] + b'\x06longer' + TRANSPARENT_BUNDLE[41:]
    original = DeserializerZcash(TRANSPARENT_BUNDLE)._zcash_transparent()
    modified = DeserializerZcash(changed)._zcash_transparent()
    assert original[0][0].script != modified[0][0].script
    assert original[3] == modified[3]


@pytest.mark.parametrize('offset', [1, 33, 41, 46, 55],
                         ids=['prev_txid', 'prev_index', 'sequence', 'value', 'pk_script'])
def test_zcash_transparent_digest_commits_to_fields(offset):
    changed = bytearray(TRANSPARENT_BUNDLE)
    changed[offset] ^= 1
    original = DeserializerZcash(TRANSPARENT_BUNDLE)._zcash_transparent()[3]
    modified = DeserializerZcash(bytes(changed))._zcash_transparent()[3]
    assert original != modified


@pytest.mark.parametrize('size', range(len(TRANSPARENT_BUNDLE)))
def test_zcash_rejects_truncated_transparent_bundle(size):
    with pytest.raises(ZcashDeserializeError):
        DeserializerZcash(TRANSPARENT_BUNDLE[:size])._zcash_transparent()


@pytest.mark.parametrize('raw,message', [
    (b'\x02' + TRANSPARENT_BUNDLE[1:], 'too many transparent inputs'),
    (b'\x00\x02' + TRANSPARENT_BUNDLE[46:], 'too many transparent outputs'),
])
def test_zcash_rejects_excessive_transparent_counts(raw, message):
    with pytest.raises(ZcashDeserializeError, match=message):
        DeserializerZcash(raw)._zcash_transparent()
