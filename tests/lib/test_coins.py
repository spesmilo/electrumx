# Tests of lib/coins.py

import pytest

from electrumx.lib.coins import Bitcoin, BitcoinCash, Coin, Komodo, NameMixin, Zcash
from electrumx.lib.script import OpCodes, Script
from electrumx.server.mempool import MemPool


@pytest.mark.parametrize('coin', [Coin, Bitcoin, BitcoinCash, Komodo])
def test_default_mempool_class(coin):
    assert coin.MEMPOOL is MemPool


def test_coin_can_override_mempool_class():
    class CustomMemPool(MemPool):
        pass

    class CustomCoin(Coin):
        MEMPOOL = CustomMemPool

    assert CustomCoin.MEMPOOL is CustomMemPool
    assert Coin.MEMPOOL is MemPool


@pytest.mark.parametrize('height,before,after', [
    (347_500, 8, 10),
    (419_200, 10, 20),
    (653_600, 20, 600),
    (903_000, 600, 200),
    (1_046_400, 200, 10),
    (1_687_104, 10, 8),
    (2_726_400, 8, 140),
    (3_146_400, 140, 100),
    (3_364_600, 100, 100),
    (3_428_143, 100, 8),
])
def test_zcash_fetch_batch_policy_boundaries(height, before, after):
    assert Zcash.max_fetch_blocks(height - 1) == before
    assert Zcash.max_fetch_blocks(height) == after
    assert Zcash.max_fetch_blocks(height + 1) == after


@pytest.mark.parametrize('height', [0, 3_428_143, 10_000_000])
def test_zcash_fetch_batch_conservative_ranges(height):
    assert Zcash.max_fetch_blocks(height) == 8


NAME = "name".encode("ascii")
DAYS = hex(6).encode("ascii")
VALUE = "value".encode("ascii")
ADDRESS_SCRIPT = "address_script".encode("ascii")

OP_NAME_NEW = OpCodes.OP_1
OP_NAME_UPDATE = OpCodes.OP_2
OP_DROP = OpCodes.OP_DROP
OP_2DROP = OpCodes.OP_2DROP
DP_MULT = NameMixin.DATA_PUSH_MULTIPLE


def create_script(pattern, address_script):
    script = bytearray()
    for item in pattern:
        if type(item) == int:
            script.append(item)
        else:
            script.extend(Script.push_data(item))
    script.extend(address_script)

    return bytes(script)


@pytest.mark.parametrize("opcode,pattern", (
        ([OP_NAME_NEW, OP_DROP, -1, -1, OP_2DROP, -1, OP_DROP],
         [OP_NAME_NEW, OP_DROP, NAME, DAYS, OP_2DROP, VALUE, OP_DROP]),

        ([OP_NAME_NEW, OP_DROP, -1, -1, OP_2DROP, DP_MULT],
         [OP_NAME_NEW, OP_DROP, NAME, DAYS, OP_2DROP, VALUE, OP_DROP]),
        ([OP_NAME_NEW, OP_DROP, -1, -1, OP_2DROP, DP_MULT],
         [OP_NAME_NEW, OP_DROP, NAME, DAYS, OP_2DROP, VALUE, VALUE, OP_2DROP]),

        ([OP_NAME_NEW, OP_DROP, -1, OP_2DROP, DP_MULT, -1, OP_DROP],
         [OP_NAME_NEW, OP_DROP, NAME, OP_2DROP, VALUE, OP_DROP, DAYS, OP_DROP]),
        ([OP_NAME_NEW, OP_DROP, -1, OP_2DROP, DP_MULT, -1, OP_DROP],
         [OP_NAME_NEW, OP_DROP, NAME, OP_2DROP, VALUE, VALUE, OP_2DROP, DAYS, OP_DROP]),
))
def test_name_mixin_interpret_name_prefix(opcode, pattern):
    ops = [opcode]
    script = create_script(pattern, ADDRESS_SCRIPT)
    parsed_names, parsed_address_script = NameMixin.interpret_name_prefix(script, ops)

    assert len(parsed_names) == 0
    assert parsed_address_script == ADDRESS_SCRIPT


@pytest.mark.parametrize("opcode,pattern", (
        ([OP_NAME_NEW, OP_DROP, "name", "days", OP_2DROP, -1, OP_DROP],
         [OP_NAME_NEW, OP_DROP, NAME, DAYS, OP_2DROP, VALUE, OP_DROP]),
        ([OP_NAME_NEW, OP_DROP, "name", OP_DROP, -1, OP_DROP, "days", OP_DROP],
         [OP_NAME_NEW, OP_DROP, NAME, OP_DROP, VALUE, OP_DROP, DAYS, OP_DROP]),

        ([OP_NAME_NEW, OP_DROP, "name", "days", OP_2DROP, DP_MULT],
         [OP_NAME_NEW, OP_DROP, NAME, DAYS, OP_2DROP, VALUE, OP_DROP]),
        ([OP_NAME_NEW, OP_DROP, "name", "days", OP_2DROP, DP_MULT],
         [OP_NAME_NEW, OP_DROP, NAME, DAYS, OP_2DROP, VALUE, VALUE, OP_2DROP]),
        ([OP_NAME_NEW, OP_DROP, "name", "days", OP_2DROP, DP_MULT],
         [OP_NAME_NEW, OP_DROP, NAME, DAYS, OP_2DROP, VALUE, VALUE, VALUE, OP_2DROP, OP_DROP]),

        ([OP_NAME_NEW, OP_DROP, "name", OP_2DROP, DP_MULT, "days", OP_DROP],
         [OP_NAME_NEW, OP_DROP, NAME, OP_2DROP, VALUE, OP_DROP, DAYS, OP_DROP]),
        ([OP_NAME_NEW, OP_DROP, "name", OP_2DROP, DP_MULT, "days", OP_DROP],
         [OP_NAME_NEW, OP_DROP, NAME, OP_2DROP, VALUE, VALUE, OP_2DROP, DAYS, OP_DROP]),
        ([OP_NAME_NEW, OP_DROP, "name", OP_2DROP, DP_MULT, "days", OP_DROP],
         [OP_NAME_NEW, OP_DROP, NAME, OP_2DROP, VALUE, VALUE, VALUE, OP_2DROP, OP_DROP, DAYS, OP_DROP]),
))
def test_name_mixin_interpret_name_prefix_with_named_placeholders(opcode, pattern):
    ops = [opcode]
    script = create_script(pattern, ADDRESS_SCRIPT)
    parsed_names, parsed_address_script = NameMixin.interpret_name_prefix(script, ops)

    assert parsed_names["name"][1] == NAME
    assert parsed_names["days"][1] == DAYS
    assert parsed_address_script == ADDRESS_SCRIPT


@pytest.mark.parametrize("opcode", (
        [OP_NAME_UPDATE, OP_DROP, -1, -1, OP_2DROP, -1, OP_DROP],
        [OP_NAME_NEW, OP_DROP, -1, -1, OP_DROP, OP_DROP, -1, OP_DROP],
        [OP_NAME_NEW, OP_DROP, "name", "days", OP_DROP, -1, OP_DROP],
))
def test_name_mixin_interpret_name_prefix_wrong_ops(opcode):
    ops = [opcode]
    script = create_script([OP_NAME_NEW, OP_DROP, NAME, DAYS, OP_2DROP,
                            VALUE, OP_DROP], ADDRESS_SCRIPT)
    parsed_names, parsed_address_script = NameMixin.interpret_name_prefix(script, ops)

    assert parsed_names is None
    assert parsed_address_script == script
