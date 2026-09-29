"""Zcash transaction decoding and wire-format validation."""

import json
from hashlib import blake2b
from pathlib import Path

import pytest

from electrumx.lib.coins import Zcash
from electrumx.lib.hash import double_sha256, hash_to_hex_str, hex_str_to_hash
from electrumx.lib.tx import DeserializerZcash, TxZcash, ZcashDeserializeError


FIXTURES = sorted((Path(__file__).parents[1] / 'transactions').glob('zcash_*.json'))
BLOCK_FIXTURES = [
    path for path in sorted((Path(__file__).parents[1] / 'blocks').glob('zcash_mainnet_*.json'))
    if 'merkle_root' in json.loads(path.read_text())
]


@pytest.mark.parametrize('fixture_path', FIXTURES, ids=lambda path: path.stem)
def test_zcash_v5_v6_transaction(fixture_path):
    fixture = json.loads(fixture_path.read_text())
    raw_tx = bytes.fromhex(fixture['hex'])
    deserializer = DeserializerZcash(raw_tx)
    tx = deserializer.read_tx()

    assert isinstance(tx, TxZcash)
    assert deserializer.cursor == len(raw_tx)
    assert hash_to_hex_str(tx.txid_rev) == fixture['txid']
    assert tx.wtxid_rev == tx.txid_rev
    with pytest.raises(ValueError, match='serializing'):
        tx.serialize()
    assert tx.version == fixture['version']
    assert tx.version_group_id == fixture['version_group_id']
    assert tx.consensus_branch_id == fixture['consensus_branch_id']
    assert tx.locktime == fixture['locktime']
    assert tx.expiry_height == fixture['expiry_height']
    assert tx.fee_adjustment == fixture['fee_adjustment_zat']

    assert len(tx.inputs) == len(fixture['inputs'])
    for expected, actual in zip(fixture['inputs'], tx.inputs):
        assert actual.sequence == expected['sequence']
        if expected['coinbase'] is not None:
            assert actual.is_generation()
            assert actual.script.hex() == expected['coinbase']
        else:
            assert hash_to_hex_str(actual.prev_txid_rev) == expected['txid']
            assert actual.prev_idx == expected['vout']
            assert actual.script.hex() == expected['script']

    assert [(output.value, output.pk_script.hex()) for output in tx.outputs] == [
        (output['value_zat'], output['pk_script']) for output in fixture['outputs']
    ]
    _, size = DeserializerZcash(raw_tx).read_tx_and_vsize()
    assert size == len(raw_tx)

    offset_reader = DeserializerZcash(b'\x00' + raw_tx, start=1)
    assert offset_reader.read_tx() == tx
    assert offset_reader.cursor == len(raw_tx) + 1


@pytest.mark.parametrize('fixture_path', FIXTURES, ids=lambda path: path.stem)
def test_zcash_v5_v6_rejects_invalid_encoding(fixture_path):
    raw = bytes.fromhex(json.loads(fixture_path.read_text())['hex'])
    with pytest.raises(ZcashDeserializeError, match='unknown Zcash transaction format'):
        DeserializerZcash(raw[:4] + bytes(4) + raw[8:]).read_tx()
    with pytest.raises(ZcashDeserializeError, match='non-canonical CompactSize'):
        DeserializerZcash(raw[:20] + b'\xfd\x01\x00' + raw[21:]).read_tx()
    with pytest.raises(ZcashDeserializeError, match='truncated'):
        DeserializerZcash(raw[:-1]).read_tx()


@pytest.mark.parametrize('fixture_path', BLOCK_FIXTURES, ids=lambda path: path.stem)
def test_zcash_block_consumption_and_merkle_root(fixture_path):
    fixture = json.loads(fixture_path.read_text())
    raw_block = bytes.fromhex(fixture['block'])
    header = Zcash.block_header(raw_block, fixture['height'])
    deserializer = DeserializerZcash(raw_block, start=len(header))
    transactions = deserializer.read_tx_block()

    assert deserializer.cursor == len(raw_block)
    hashes = [tx.txid_rev for tx in transactions]
    while len(hashes) > 1:
        if len(hashes) & 1:
            hashes.append(hashes[-1])
        hashes = [double_sha256(left + right) for left, right in zip(hashes[::2], hashes[1::2])]
    assert hashes[0] == hex_str_to_hash(fixture['merkle_root'])


@pytest.mark.parametrize('name', ('zcash_mainnet_f9369e', 'zcash_mainnet_f15bfc'))
def test_zcash_effecting_and_authorizing_bytes_have_expected_txid_behavior(name):
    fixture_path = next(path for path in FIXTURES if path.stem == name)
    fixture = json.loads(fixture_path.read_text())
    raw_tx = bytes.fromhex(fixture['hex'])

    # The final binding signature is authorizing data, so it must not affect
    # ZIP-244's transaction ID.
    changed_signature = raw_tx[:-1] + bytes((raw_tx[-1] ^ 1,))
    changed_txid = DeserializerZcash(changed_signature).read_tx().txid_rev
    assert hash_to_hex_str(changed_txid) == fixture['txid']

    # lockTime is in the effecting header digest and must affect the txid.
    changed_locktime = raw_tx[:12] + bytes((raw_tx[12] ^ 1,)) + raw_tx[13:]
    changed_txid = DeserializerZcash(changed_locktime).read_tx().txid_rev
    assert hash_to_hex_str(changed_txid) != fixture['txid']


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


SAPLING_SPEND = b'C' * 32 + b'N' * 32 + b'R' * 32
SAPLING_OUTPUT = (b'V' * 32 + b'M' * 32 + b'E' * 32 + b'C' * 52 +
                  b'D' * 512 + b'T' * 16 + b'O' * 80)


def sapling_v5_wire(spends=1, outputs=1, balance=12345):
    raw = bytes([spends]) + SAPLING_SPEND * spends + bytes([outputs]) + SAPLING_OUTPUT * outputs
    if not (spends or outputs):
        return raw
    return (raw + balance.to_bytes(8, 'little', signed=True) +
            (b'A' * 32 if spends else b'') + b'P' * (192 * spends) +
            b'S' * (64 * spends) + b'Q' * (192 * outputs) + b'B' * 64)


def zip244_hash(person, data=b''):
    return blake2b(data, digest_size=32, person=person).digest()


@pytest.mark.parametrize('spends,outputs', [(0, 0), (1, 0), (0, 1), (1, 1), (2, 2)])
@pytest.mark.parametrize('balance', [-12345, 0, 12345])
def test_zcash_v5_sapling_digest_and_balance(spends, outputs, balance):
    raw = sapling_v5_wire(spends, outputs, balance)
    reader = DeserializerZcash(raw + b'next')
    digest, value_balance = reader._zcash_sapling(5)
    assert value_balance == (balance if spends or outputs else 0)
    assert reader.cursor == len(raw)
    assert reader._zcash_read(4) == b'next'

    # Reference digest assembled from named synthetic fields, not wire slices.
    spend_data = b''
    if spends:
        spend_data = (
            zip244_hash(b'ZTxIdSSpendCHash', b'N' * (32 * spends)) +
            zip244_hash(b'ZTxIdSSpendNHash', (b'C' * 32 + b'A' * 32 + b'R' * 32) * spends))
    output_data = b''
    if outputs:
        output_data = (
            zip244_hash(b'ZTxIdSOutC__Hash', (b'M' * 32 + b'E' * 32 + b'C' * 52) * outputs) +
            zip244_hash(b'ZTxIdSOutM__Hash', b'D' * (512 * outputs)) +
            zip244_hash(b'ZTxIdSOutN__Hash', (b'V' * 32 + b'T' * 16 + b'O' * 80) * outputs))
    data = b''
    if spends or outputs:
        data = (zip244_hash(b'ZTxIdSSpendsHash', spend_data) +
                zip244_hash(b'ZTxIdSOutputHash', output_data) +
                balance.to_bytes(8, 'little', signed=True))
    assert digest == zip244_hash(b'ZTxIdSaplingHash', data)


@pytest.mark.parametrize('offset', [1, 33, 65, 98, 130, 162, 194, 246, 758, 774, 854, 862],
                         ids=['spend_cv', 'nullifier', 'rk', 'output_cv', 'cmu', 'epk',
                              'ciphertext', 'memo', 'tag', 'out_ciphertext', 'balance', 'anchor'])
def test_zcash_v5_sapling_digest_commits_to_fields(offset):
    raw = sapling_v5_wire()
    changed = bytearray(raw)
    changed[offset] ^= 1
    original = DeserializerZcash(raw)._zcash_sapling(5)[0]
    assert DeserializerZcash(bytes(changed))._zcash_sapling(5)[0] != original


@pytest.mark.parametrize('offset', [894, 1086, 1150, 1342],
                         ids=['spend_proof', 'spend_auth_sig', 'output_proof', 'binding_sig'])
def test_zcash_v5_sapling_digest_excludes_authorizing_data(offset):
    raw = sapling_v5_wire()
    changed = bytearray(raw)
    changed[offset] ^= 1
    assert (DeserializerZcash(raw)._zcash_sapling(5) ==
            DeserializerZcash(bytes(changed))._zcash_sapling(5))


@pytest.mark.parametrize('size', [0, 1, 96, 97, 98, 853, 854, 861, 862, 893, 894,
                                1085, 1086, 1149, 1150, 1341, 1342, 1405])
def test_zcash_rejects_truncated_v5_sapling_bundle(size):
    with pytest.raises(ZcashDeserializeError):
        DeserializerZcash(sapling_v5_wire()[:size])._zcash_sapling(5)


@pytest.mark.parametrize('raw,message', [
    (b'\x02' + SAPLING_SPEND, 'too many Sapling spends'),
    (b'\x00\x02' + SAPLING_OUTPUT, 'too many Sapling outputs'),
])
def test_zcash_rejects_excessive_sapling_counts(raw, message):
    with pytest.raises(ZcashDeserializeError, match=message):
        DeserializerZcash(raw)._zcash_sapling(5)


ORCHARD_ACTION = (b'C' * 32 + b'N' * 32 + b'R' * 32 + b'M' * 32 + b'E' * 32 +
                  b'C' * 52 + b'D' * 512 + b'T' * 16 + b'O' * 80)


def orchard_v5_wire(actions=1, flags=3, balance=12345, proof=b'prf'):
    # Synthetic proof/signatures: these tests check encoding, not validity.
    raw = bytes([actions]) + ORCHARD_ACTION * actions
    if not actions:
        return raw
    return (raw + bytes([flags]) + balance.to_bytes(8, 'little', signed=True) +
            b'A' * 32 + bytes([len(proof)]) + proof + b'S' * (64 * actions) + b'B' * 64)


@pytest.mark.parametrize('actions,flags', [(0, 0), (1, 0), (1, 1), (1, 2), (1, 3), (2, 3)])
@pytest.mark.parametrize('balance', [-12345, 0, 12345])
def test_zcash_v5_orchard_digest_and_balance(actions, flags, balance):
    raw = orchard_v5_wire(actions, flags, balance)
    reader = DeserializerZcash(raw + b'next')
    digest, value_balance = reader._zcash_action_bundle(5)
    assert value_balance == (balance if actions else 0)
    assert reader.cursor == len(raw)
    assert reader._zcash_read(4) == b'next'

    # Build the reference digest from named fields rather than wire slices.
    data = b''
    if actions:
        data = (
            zip244_hash(b'ZTxIdOrcActCHash',
                        (b'N' * 32 + b'M' * 32 + b'E' * 32 + b'C' * 52) * actions) +
            zip244_hash(b'ZTxIdOrcActMHash', b'D' * (512 * actions)) +
            zip244_hash(b'ZTxIdOrcActNHash',
                        (b'C' * 32 + b'R' * 32 + b'T' * 16 + b'O' * 80) * actions) +
            bytes([flags]) + balance.to_bytes(8, 'little', signed=True) + b'A' * 32)
    assert digest == zip244_hash(b'ZTxIdOrchardHash', data)


@pytest.mark.parametrize('offset', [1, 33, 65, 97, 129, 161, 213, 725, 741, 821, 822, 830],
                         ids=['cv', 'nullifier', 'rk', 'cmx', 'epk', 'ciphertext', 'memo',
                              'tag', 'out_ciphertext', 'flags', 'balance', 'anchor'])
def test_zcash_v5_orchard_digest_commits_to_fields(offset):
    raw = orchard_v5_wire()
    changed = bytearray(raw)
    changed[offset] ^= 1
    assert (DeserializerZcash(raw)._zcash_action_bundle(5)[0] !=
            DeserializerZcash(bytes(changed))._zcash_action_bundle(5)[0])


@pytest.mark.parametrize('offset', [863, 866, 930], ids=['proof', 'spend_auth_sig', 'binding_sig'])
def test_zcash_v5_orchard_digest_excludes_authorizing_data(offset):
    raw = orchard_v5_wire()
    changed = bytearray(raw)
    changed[offset] ^= 1
    assert (DeserializerZcash(raw)._zcash_action_bundle(5) ==
            DeserializerZcash(bytes(changed))._zcash_action_bundle(5))


def test_zcash_v5_orchard_digest_excludes_proof_length():
    assert (DeserializerZcash(orchard_v5_wire())._zcash_action_bundle(5) ==
            DeserializerZcash(orchard_v5_wire(proof=b'longer proof'))._zcash_action_bundle(5))


@pytest.mark.parametrize('flags', [4, 8, 16, 32, 64, 128, 255])
def test_zcash_rejects_invalid_v5_orchard_flags(flags):
    with pytest.raises(ZcashDeserializeError, match='invalid Orchard-protocol flags'):
        DeserializerZcash(orchard_v5_wire(flags=flags))._zcash_action_bundle(5)


@pytest.mark.parametrize('size', [0, 1, 820, 821, 829, 830, 861, 862, 863, 865, 866, 929, 930, 993])
def test_zcash_rejects_truncated_v5_orchard_bundle(size):
    with pytest.raises(ZcashDeserializeError):
        DeserializerZcash(orchard_v5_wire()[:size])._zcash_action_bundle(5)


def test_zcash_rejects_excessive_orchard_actions():
    with pytest.raises(ZcashDeserializeError, match='too many Orchard-protocol actions'):
        DeserializerZcash(b'\x02' + ORCHARD_ACTION)._zcash_action_bundle(5)


def test_zcash_rejects_noncanonical_orchard_proof_length():
    raw = orchard_v5_wire()
    changed = raw[:862] + b'\xfd\x03\x00' + raw[863:]
    with pytest.raises(ZcashDeserializeError, match='non-canonical CompactSize'):
        DeserializerZcash(changed)._zcash_action_bundle(5)


@pytest.mark.parametrize('bundle', ['sapling', 'orchard', 'ironwood'])
def test_zcash_v6_digest_excludes_anchor(bundle):
    raw = sapling_v5_wire() if bundle == 'sapling' else orchard_v5_wire()
    changed = bytearray(raw)
    changed[862 if bundle == 'sapling' else 830] ^= 1

    def read(data):
        reader = DeserializerZcash(data)
        if bundle == 'sapling':
            return reader._zcash_sapling(6)
        return reader._zcash_action_bundle(6, ironwood=bundle == 'ironwood')

    assert read(raw) == read(bytes(changed))


@pytest.mark.parametrize('flags', [4, 7, 8, 255])
def test_zcash_v6_ironwood_flag_rules(flags):
    raw = orchard_v5_wire(flags=flags)
    with pytest.raises(ZcashDeserializeError, match='invalid Orchard-protocol flags'):
        DeserializerZcash(raw)._zcash_action_bundle(6)
    if flags < 8:
        assert DeserializerZcash(raw)._zcash_action_bundle(6, ironwood=True)[1] == 12345
    else:
        with pytest.raises(ZcashDeserializeError, match='invalid Orchard-protocol flags'):
            DeserializerZcash(raw)._zcash_action_bundle(6, ironwood=True)
