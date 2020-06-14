import random

from electrumx.server import utreexo



def test_utreexo():
    utreexo.Hash = lambda x: '('+x+')'    
    f = utreexo.Forest()
    acc = utreexo.Accumulator()
    leaves = ['1', '2', '3', '4', '5', '6', '7']
    for l in leaves:
        acc.add(l)
        f.add(l)
        print('adding', l, ': ', acc.dump())
    print('------')
    random.shuffle(leaves)
    #leaves = ['7', '6']
    print('deleting', leaves)
    f.batch_delete(leaves)
    print('f: ', f.dump())
    #for l in leaves:
    #    p = f.get_proof(l)
    #    f.remove(l)
    #    acc.delete(l, p)
    #    assert acc.dump() == f.dump()
    #    print('removing', l, ': ', f.dump())
    #assert len(f.utxos) == 0

    #
def test_hash(x: bytes, y=None):
    s = x.strip(b'0').decode()
    if y:
        s2 = y.strip(b'0').decode()
    else:
        s2 = ''
    s = '('+ s + s2 + ')'
    return s.encode().zfill(utreexo.HSIZE)


def hash2str(x):
    return x.decode().strip('0')
    
def dump(acc):
    for h in range(len(acc)):
        print('%d:'% h, hash2str(acc[h].get_root()))

def dump_leaves(acc):
    print('leaves')
    for h in range(len(acc)):
        print('%d:'% h, [hash2str(x) for x in acc[h].get_leaves()])

def dump_utxos(F):
    for k, s in F.utxos.items():
        h = len(s)
        leaf = F.acc[h].read_tree(s)
        assert leaf == k, (leaf, k)
        print(hash2str(k), '->', s)

        
def test_utreexo2():
    utreexo.Hash = test_hash
    utreexo.HSIZE = 40
    F = utreexo.HashForest()
    items = ['1', '2', '3', '4', '5', '6', '7', '8', '9']
    for l in items:
        print('adding', l, ': ')
        F.add(l.encode())
        dump(F.acc)
    print('------')
    dump_leaves(F.acc)
    print('------')
    dump_utxos(F)
    print('------')
    random.shuffle(items)
    for l in items:
        print('deleting', l)
        F.remove(l.encode())
        dump(F.acc)

if __name__ == '__main__':

    test_utreexo()
