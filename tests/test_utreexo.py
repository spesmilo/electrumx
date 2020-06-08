import random

from electrumx.server import utreexo

utreexo.sha256 = lambda x: '('+x+')'


def test_utreexo():
    acc = utreexo.Forest()
    leaves = ['1', '2', '3', '4', '5', '6', '7', '8']
    for l in leaves:
        acc.add(l)
    print('acc', acc.dump())
    acc.verify_leaf('2')
    acc.verify_leaf('5')
    print('------')
    random.shuffle(leaves)
    print(leaves)
    for l in leaves:
        p = acc.get_proof(l)
        print('removing', l, p)
        acc.remove(l)
        print('acc', acc.dump())
    assert len(acc.nodes) == 0


if __name__ == '__main__':
    test_utreexo()
