#
# convention: a parent has two children
#
#    parent
#     / \
#    /   \
#   c1   c2   children
#

from electrumx.lib.hash import sha256


class Accumulator:

    def __init__(self):
        self.acc = {}     # n -> hash
        self.counter = 0

    def leaf(self, utxo):
        return sha256(utxo)

    def parent(self, x, y, is_left):
        # is_left: whether y is the left leaf
        if is_left:
            x, y = y, x
        return sha256(x + y)

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
        self._hash = sha256(utxo)

class Parent(Node):
    def __init__(self, x, y):
        self.parent = None
        self.left = x
        self.right = y
        x.parent = self
        y.parent = self
        self._hash = sha256(x._hash + y._hash)


class Forest:

    def __init__(self):
        self.acc = {}     # n -> hash
        self.counter = 0
        self.utxos = {}   # hash -> Node

    def get_leaf(self, utxo):
        return self.utxos.get(sha256(utxo))

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
        self.utxos[leaf._hash] = leaf
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
        node = self.get_leaf(utxo)
        self.utxos.pop(node._hash)

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
