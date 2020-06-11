#
# convention: a parent has two children
#
#    parent
#     / \
#    /   \
#   c1   c2   children
#


from electrumx.lib.hash import blake2b

def Hash(x, y=b''):
    return blake2b(x+y)


class Accumulator:

    def __init__(self):
        self.acc = {}     # n -> hash
        self.counter = 0

    def leaf(self, utxo):
        return Hash(utxo)

    def parent(self, x, y, is_left):
        # is_left: whether y is the left leaf
        if is_left:
            x, y = y, x
        return Hash(x + y)

    def add(self, utxo):
        n = self.leaf(utxo)
        h = 0
        r = self.acc.pop(h, None)
        while r != None:
            n = self.parent(r, n, False)  # n is not left
            h += 1
            r = self.acc.pop(h, None)
        self.acc[h] = n
        self.counter += 1

    def verify(self, utxo, proof):
        n = self.leaf(utxo)
        h = 0
        while h < len(proof):
            p, is_left = proof[h]
            n = self.parent(p, n, is_left)
            h += 1
        assert self.acc.get(h)._hash == n, (self.acc.get(h), n)

    def delete(self, utxo, proof):
        n = None
        h = 0
        while h < len(proof):
            p, is_left = proof[h]
            if n is not None:
                n = self.parent(p, n, is_left)
            else:
                r = self.acc.pop(h, None)
                if r is None:
                    self.acc[h] = p
                else:
                    n = self.parent(p, r, is_left)
            h += 1
        self.acc[h] = n
        self.counter -= 1

    def dump(self):
        n = max(self.acc.keys())
        return [self.acc.get(i) for i in range(0, n + 1)]


#######################

class Node:
    def sibling(self):
        assert self.parent is not None
        if self.parent.right is self:
            return self.parent.left, False
        else:
            return self.parent.right, True

class Leaf(Node):
    def __init__(self, utxo):
        self.parent = None
        self._hash = Hash(utxo)

class Parent(Node):
    def __init__(self, x, y):
        self.parent = None
        self.left = x
        self.right = y
        x.parent = self
        y.parent = self
        self._hash = Hash(x._hash + y._hash)


class Forest:

    def __init__(self):
        self.acc = {}     # n -> hash
        self.counter = 0
        self.utxos = {}   # hash -> Node

    def get_leaf(self, utxo):
        return self.utxos.get(utxo)

    def get_proof(self, utxo):
        l = self.get_leaf(utxo)
        proof = []
        while l.parent is not None:
            S, b = l.sibling()
            proof.append((S._hash, b))
            l = l.parent
        return proof

    def add_leaf(self, utxo):
        leaf = Leaf(utxo)
        self.utxos[utxo] = leaf
        return leaf

    def add_parent(self, x, y, is_left):
        if is_left:
            x, y = y, x
        parent = Parent(x, y)
        return parent

    def add(self, utxo):
        n = self.add_leaf(utxo)
        h = 0
        r = self.acc.pop(h, None)
        while r != None:
            n = self.add_parent(r, n, False)  # n is not left
            h += 1
            r = self.acc.pop(h, None)
        self.acc[h] = n
        self.counter += 1

    def verify_leaf(self, utxo):
        proof = self.get_proof(utxo)
        self.verify(utxo, proof)

    def remove(self, utxo):
        n = None
        h = 0
        N = self.get_leaf(utxo)
        while N.parent is not None:
            P, is_left = N.sibling()
            if n is not None:
                n = self.add_parent(P, n, is_left)
            else:
                r = self.acc.pop(h, None)
                if r is None:
                    self.acc[h] = P
                    P.parent = None
                else:
                    n = self.add_parent(P, r, is_left)
            h += 1
            N = N.parent
        self.acc[h] = n
        self.counter -= 1
        # we need to store the proof, for block verification
        self.utxos.pop(utxo)

    def serialize_utxo(self, tx_hash: bytes, index: int):
        return tx_hash[::-1] + index.to_bytes(4, 'big')

    def add_utxo(self, tx_hash, index):
        self.add(self.serialize_utxo(tx_hash, index))

    def remove_utxo(self, tx_hash, index):
        self.remove(self.serialize_utxo(tx_hash, index))

    def dump(self):
        n = max(self.acc.keys())
        roots = [self.acc.get(i) for i in range(0, n + 1)]
        return [r._hash if r else None for r in roots]



######################
#
#  


from io import BytesIO

    
HSIZE = 32


def treesize(h):
    return pow(2, h+1) - 1


def first_zero_bit(n):
    i = 0
    while n % 2:
        n = n >> 1
        i += 1
    return i


class HashTree:

    def __init__(self, h):
        self.h = h
        self.size = treesize(self.h)
        self.zero = bytearray().zfill(HSIZE)
        data = bytearray().zfill(self.size * HSIZE)
        self.data = BytesIO(data)

    def read(self, pos, n):
        self.data.seek(pos*HSIZE)
        return self.data.read(n*HSIZE)
        #return self.data[pos*HSIZE:(pos + n)*HSIZE]

    def write(self, pos, data):
        self.data.seek(pos*HSIZE)
        return self.data.write(data)
        #self.data[pos*HSIZE:pos*HSIZE + len(data)] = data

    def get_data(self):
        return self.read(0, self.size)

    def get_hash(self, index):
        return self.read(index, 1)

    def set_hash(self, index, _hash):
        assert len(_hash) == HSIZE, (len(_hash), HSIZE)
        self.write(index, _hash)

    def get_root(self):
        return self.get_hash(self.size - 1)

    def set_root(self, _hash):
        return self.set_hash(self.size - 1, _hash)

    def blank(self):
        self.set_root(self.zero)

    def is_empty(self):
        return self.get_root() == self.zero

    def get_offset(self, s):
        size = treesize(self.h - len(s))
        offset = 0
        for i in range(len(s)):
            if s[i] == '1':
                offset += treesize(self.h - 1 - i)
        return offset, size

    def write_tree(self, s, data):
        offset, size = self.get_offset(s)
        assert len(data) == size *HSIZE
        self.write(offset, data)

    def read_tree(self, s):
        offset, size = self.get_offset(s)
        return self.read(offset, size)

    def read_root(self, s):
        offset, size = self.get_offset(s)
        return self.read(offset + size - 1, 1)

    def write_root(self, s, data):
        offset, size = self.get_offset(s)
        self.write(offset + size - 1, data)

    def update_root(self, s):
        r1 = self.read_root(s + '0')
        r2 = self.read_root(s + '1')
        self.write_root(s, Hash(r1, r2))

    def get_leaves(self, s=''):
        if len(s) == self.h:
            return [bytes(self.read_tree(s))]
        else:
            l1 = self.get_leaves(s + '0')
            l2 = self.get_leaves(s + '1')
            return l1 + l2

    def maybe_get_leaves(self):
        self.data.seek(0)
        for i in range(self.size):
            yield self.data.read(HSIZE)

"""
indices of leaves:     
0 : 0
1 : 0, 1
2 : 0, 1, -, 3, 4, -, -
3 : 0, 1, -, 3, 4, -, -, 7, 8, -, 10, 11, -, -, -

0 : 0
1 : -, 1, 2
2 : -, -, 



size(h) = pow(2, h+1) - 1

offset(s) = sum( s_i * size(i) )


simpler mapping:
 - each row has the same width

0

0, 
1, 
(01)

0, 1, (01)
2, 3, (23)
(0123), -, -


    size(h) = size(h-1) * 3
=>  size(h) = pow(3,h)


def offset(s):

   size(h)


"""

FORBIDDEN = [
    bytes.fromhex('d5d27987d2a3dfc724e359870c6644b40e497bdc0589a033220fe15429d88599')[::-1],
    bytes.fromhex('e3bf3d07d4b0375638d5f1db5255fe07ba2c4cb067cd81b84ee974b6585fb468')[::-1]
]



class HashForest:

    def __init__(self):
        self.acc = {}
        self.counter = 0
        self.utxos = {} # hash -> index

    def get_hashtree(self, h):
        # allocate data if needed
        if h not in self.acc:
            self.acc[h] = HashTree(h)
        return self.acc[h]

    def decrement_indices(self, r, prefix):
        n = len(prefix)
        #for l in r.get_leaves([]):
        for l in r.maybe_get_leaves():
            s = self.utxos.get(l)
            if s is None:
                continue
            assert s[0:n] == prefix
            #s >> n
            s = s[n:]
            self.utxos[l] = s

    def increment_indices(self, r, prefix):
        n = len(prefix)
        #for l in r.get_leaves([]):
        for l in r.maybe_get_leaves():
            s = self.utxos.get(l)
            if s is None:
                continue
            self.utxos[l] = prefix + s

    def add(self, utxo):
        target = self.get_hashtree(first_zero_bit(self.counter))
        _hash = Hash(utxo)
        # write leaf into target
        s = '0'*target.h
        target.write_tree(s, _hash)
        self.utxos[_hash] = s
        for h in range(target.h):
            r = self.acc[h]
            s = s[0:-1]
            target.write_tree(s + '1', r.get_data())
            target.update_root(s)
            self.increment_indices(r, s + '1')
            r.blank()
        self.counter += 1

    def remove(self, utxo):
        utxo_hash = Hash(utxo)
        s = self.utxos.pop(utxo_hash)
        target_h = len(s)
        target = self.acc[target_h]
        assert target.read_tree(s) == utxo_hash
        n = None
        h = 0
        for h in range(target_h):
            parent, is_left = s[0:-1], s[-1]
            if n is not None:
                target.update_root(parent)
                n = parent
            else:
                r = self.acc[h]
                if r.is_empty():
                    sibling = parent + ('1' if is_left == '0' else '0')
                    data = target.read_tree(sibling)
                    r.write_tree('', data)
                    self.decrement_indices(r, sibling) # remove parent path to leaves of r 
                else:
                    target.write_tree(s, r.get_data())
                    target.update_root(parent) # should maybe update all roots..
                    self.increment_indices(r, s) # prepend parent path to indices of r
                    n = parent
                    r.blank()
            s = parent
        if n is not None:
            n_data = target.read_tree(n)
            self.acc[target_h].write_tree('', n_data)
        else:
            self.acc[target_h].blank()
        self.counter -= 1
        # we need to store the proof, for block verification

    def serialize_utxo(self, tx_hash: bytes, index: int):
        return tx_hash[::-1] + index.to_bytes(4, 'big')

    def add_utxo(self, tx_hash, index):
        if tx_hash not in FORBIDDEN:
            self.add(self.serialize_utxo(tx_hash, index))

    def remove_utxo(self, tx_hash, index):
        if tx_hash not in FORBIDDEN:
            self.remove(self.serialize_utxo(tx_hash, index))
