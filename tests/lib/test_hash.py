#
# Tests of lib/hash.py
#
from functools import partial

import pytest

import electrumx.lib.hash as lib_hash


def test_sha256():
    assert lib_hash.sha256(b'sha256') == b'][\t\xf6\xdc\xb2\xd5:_\xff\xc6\x0cJ\xc0\xd5_\xab\xdfU`i\xd6c\x15E\xf4*\xa6\xe3P\x0f.'
    with pytest.raises(TypeError):
        lib_hash.sha256('sha256')

def test_double_sha256():
    assert lib_hash.double_sha256(b'double_sha256') == b'ksn\x8e\xb7\xb9\x0f\xf6\xd9\xad\x88\xd9#\xa1\xbcU(j1Bx\xce\xd5;s\xectL\xe7\xc5\xb4\x00'

def test_hash_to_hex_str():
    assert lib_hash.hash_to_hex_str(b'hash_to_str') == '7274735f6f745f68736168'

def test_hex_str_to_hash():
    assert lib_hash.hex_str_to_hash('7274735f6f745f68736168') == b'hash_to_str'

def test_Base58_char_value():
    chars = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
    for value, c in enumerate(chars):
        assert lib_hash.Base58.char_value(c) == value
    for c in (' ', 'I', '0', 'l', 'O'):
        with pytest.raises(lib_hash.Base58Error):
            lib_hash.Base58.char_value(c)

def test_Base58_decode():
    with pytest.raises(TypeError):
        lib_hash.Base58.decode(b'foo')
    with pytest.raises(lib_hash.Base58Error):
        lib_hash.Base58.decode('')
    assert lib_hash.Base58.decode('123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz') == b'\x00\x01\x11\xd3\x8e_\xc9\x07\x1f\xfc\xd2\x0bJv<\xc9\xaeO%+\xb4\xe4\x8f\xd6j\x83^%*\xda\x93\xffH\rm\xd4=\xc6*d\x11U\xa5'
    assert lib_hash.Base58.decode('3i37NcgooY8f1S') == b'0123456789'

def test_Base58_encode():
    with pytest.raises(TypeError):
        lib_hash.Base58.encode('foo')
    assert lib_hash.Base58.encode(b'') == ''
    assert lib_hash.Base58.encode(b'\0') == '1'
    assert lib_hash.Base58.encode(b'0123456789') == '3i37NcgooY8f1S'

def test_Base58_decode_check():
    with pytest.raises(TypeError):
        lib_hash.Base58.decode_check(b'foo')
    assert lib_hash.Base58.decode_check('4t9WKfuAB8') == b'foo'
    with pytest.raises(lib_hash.Base58Error):
        lib_hash.Base58.decode_check('4t9WKfuAB9')

def test_Base58_encode_check():
    with pytest.raises(TypeError):
        lib_hash.Base58.encode_check('foo')
    assert lib_hash.Base58.encode_check(b'foo') == '4t9WKfuAB8'

def test_Base58_decode_check_custom():
    decode_check_sha256 = partial(lib_hash.Base58.decode_check,
                                  hash_fn=lib_hash.sha256)
    with pytest.raises(TypeError):
        decode_check_sha256(b'foo')
    assert decode_check_sha256('4t9WFhKfWr') == b'foo'
    with pytest.raises(lib_hash.Base58Error):
        decode_check_sha256('4t9WFhKfWp')

def test_Base58_encode_check_custom():
    encode_check_sha256 = partial(lib_hash.Base58.encode_check,
                                  hash_fn=lib_hash.sha256)
    with pytest.raises(TypeError):
        encode_check_sha256('foo')
    assert encode_check_sha256(b'foo') == '4t9WFhKfWr'


# Bech32/Bech32m tests (BIP173/BIP350)

def test_Bech32_decode_p2wpkh():
    # P2WPKH address (witness version 0, 20-byte program)
    hrp, version, program = lib_hash.Bech32.decode(
        'bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4'
    )
    assert hrp == 'bc'
    assert version == 0
    assert program == bytes.fromhex('751e76e8199196d454941c45d1b3a323f1433bd6')

def test_Bech32_decode_p2wsh():
    # P2WSH address (witness version 0, 32-byte program)
    hrp, version, program = lib_hash.Bech32.decode(
        'bc1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3qccfmv3'
    )
    assert hrp == 'bc'
    assert version == 0
    assert len(program) == 32

def test_Bech32_decode_p2tr():
    # P2TR address (witness version 1, 32-byte program) - uses bech32m
    hrp, version, program = lib_hash.Bech32.decode(
        'bc1p0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7vqzk5jj0'
    )
    assert hrp == 'bc'
    assert version == 1
    assert len(program) == 32

def test_Bech32_decode_testnet_p2tr():
    # Testnet P2TR address
    hrp, version, program = lib_hash.Bech32.decode(
        'tb1pqqqqp399et2xygdj5xreqhjjvcmzhxw4aywxecjdzew6hylgvsesf3hn0c'
    )
    assert hrp == 'tb'
    assert version == 1
    assert len(program) == 32

def test_Bech32_encode_p2wpkh():
    # Encode P2WPKH
    addr = lib_hash.Bech32.encode(
        'bc', 0, bytes.fromhex('751e76e8199196d454941c45d1b3a323f1433bd6')
    )
    assert addr == 'bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4'

def test_Bech32_encode_p2tr():
    # Encode P2TR (should use bech32m)
    program = bytes.fromhex('79be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798')
    addr = lib_hash.Bech32.encode('bc', 1, program)
    # Decode it back to verify roundtrip
    hrp, version, decoded_program = lib_hash.Bech32.decode(addr)
    assert hrp == 'bc'
    assert version == 1
    assert decoded_program == program

def test_Bech32_invalid_checksum():
    with pytest.raises(lib_hash.Bech32Error):
        lib_hash.Bech32.decode('bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t5')  # wrong checksum

def test_Bech32_mixed_case():
    with pytest.raises(lib_hash.Bech32Error):
        lib_hash.Bech32.decode('bc1qW508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4')  # mixed case

def test_Bech32_wrong_encoding_v0():
    # Witness v0 with bech32m encoding should fail
    with pytest.raises(lib_hash.Bech32Error):
        # This is a v0 program encoded with bech32m (wrong)
        lib_hash.Bech32.decode('bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kemeawh')

def test_Bech32_wrong_encoding_v1():
    # Witness v1 with bech32 encoding should fail
    with pytest.raises(lib_hash.Bech32Error):
        # This is a v1 program encoded with bech32 (wrong)
        lib_hash.Bech32.decode('bc1pw508d6qejxtdg4y5r3zarvary0c5xw7kw508d6qejxtdg4y5r3zarvary0c5xw7k7grplx')

def test_Bech32_invalid_program_length():
    with pytest.raises(lib_hash.Bech32Error):
        # Too short
        lib_hash.Bech32.encode('bc', 0, b'\x00')
    with pytest.raises(lib_hash.Bech32Error):
        # v0 must be 20 or 32 bytes - 25 is invalid
        lib_hash.Bech32.decode('bc1q0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3cchs7ex')
