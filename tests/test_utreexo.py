import random

from electrumx.server import utreexo

utreexo.sha256 = lambda x: '('+x+')'


def test_utreexo():
    f = utreexo.Forest()
    acc = utreexo.Accumulator()
    leaves = ['1', '2', '3', '4', '5', '6', '7', '8']
    for l in leaves:
        acc.add(l)
        f.add(l)
        print('adding', l, ': ', acc.dump())
    print('------')
    random.shuffle(leaves)
    for l in leaves:
        p = f.get_proof(l)
        f.remove(l)
        acc.delete(l, p)
        assert acc.dump() == f.dump()
        print('removing', l, ': ', acc.dump())
    assert len(f.utxos) == 0


if __name__ == '__main__':
    test_utreexo()
