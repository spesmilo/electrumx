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

    def reset_parent(self, x):
        pass

    def add_parent(self, *args):
        return self.parent(*args)

    def add_leaf(self, *args):
        return self.leaf(*args)

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

    def verify(self, utxo, proof):
        n = self.leaf(utxo)
        h = 0
        while h < len(proof):
            p, is_left = proof[h]
            n = self.parent(p, n, is_left)
            h += 1
        assert self.acc.get(h) == n, (self.acc.get(h), n)

    def delete(self, utxo, proof):
        n = None
        h = 0
        while h < len(proof):
            p, is_left = proof[h]
            if n is not None:
                n = self.add_parent(p, n, is_left)
            else:
                r = self.acc.pop(h, None)
                if r is None:
                    self.acc[h] = p
                    self.reset_parent(p)
                else:
                    n = self.add_parent(p, r, is_left)
            h += 1
        self.acc[h] = n
        self.counter -= 1



#######################

class Node:
    def sibling(self):
        assert self.parent is not None
        if self.parent.right is self:
            return self.parent.left._hash, False
        else:
            return self.parent.right._hash, True

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


class Forest(Accumulator):
    # accumulator with proofs

    def __init__(self):
        Accumulator.__init__(self)
        self.nodes = {}   # hash -> Node

    def get_leaf(self, utxo):
        return self.nodes.get(sha256(utxo))

    def get_proof(self, utxo):
        l = self.get_leaf(utxo)
        proof = []
        while l.parent is not None:
            proof.append(l.sibling())
            l = l.parent
        return proof

    def add_leaf(self, utxo):
        leaf = Leaf(utxo)
        self.nodes[leaf._hash] = leaf
        return leaf._hash

    def reset_parent(self, x):
        self.nodes[x].parent = None

    def add_parent(self, x, y, is_left):
        if is_left:
            x, y = y, x
        node_x = self.nodes[x]
        node_y = self.nodes[y]
        parent = Parent(node_x, node_y)
        self.nodes[parent._hash] = parent
        return parent._hash

    def verify_leaf(self, utxo):
        proof = self.get_proof(utxo)
        self.verify(utxo, proof)

    def remove(self, utxo):
        proof = self.get_proof(utxo)
        self.verify(utxo, proof)
        # delete from accumulator
        self.delete(utxo, proof)
        # delete from nodes
        key = sha256(utxo)
        while True:
            node = self.nodes.pop(key)
            if node.parent is None:
                break
            key = node.parent._hash

    def serialize_utxo(self, tx_hash: bytes, index: int):
        return tx_hash[::-1] + index.to_bytes(4, 'big')

    def add_utxo(self, tx_hash, index):
        self.add(self.serialize_utxo(tx_hash, index))

    def remove_utxo(self, tx_hash, index):
        self.remove(self.serialize_utxo(tx_hash, index))

    def dump(self):
        return dict([(x, y) for x, y in self.acc.items() if y is not None])
