# Copyright (c) 2016-2017, the ElectrumX authors
#
# All rights reserved.
#
# See the file "LICENCE" for information about the copyright
# and warranty status of this software.

'''Backend database abstraction.'''

import os
from functools import partial
from typing import Type

import electrumx.lib.util as util


def db_class(name) -> Type['Storage']:
    '''Returns a DB engine class.'''
    for db_class in util.subclasses(Storage):
        if db_class.__name__.lower() == name.lower():
            db_class.import_module()
            return db_class
    raise RuntimeError(f'unrecognised DB engine "{name}"')


class Storage:
    '''Abstract base class of the DB backend abstraction.'''

    def __init__(self, name, for_sync):
        self.is_new = not os.path.exists(name)
        self.for_sync = for_sync or self.is_new
        self.open(name, create=self.is_new)

    @classmethod
    def import_module(cls):
        '''Import the DB engine module.'''
        raise NotImplementedError

    def open(self, name, create):
        '''Open an existing database or create a new one.'''
        raise NotImplementedError

    def close(self):
        '''Close an existing database.'''
        raise NotImplementedError

    def get(self, key):
        raise NotImplementedError

    def put(self, key, value):
        raise NotImplementedError

    def write_batch(self):
        '''Return a context manager that provides `put` and `delete`.

        Changes should only be committed when the context manager
        closes without an exception.
        '''
        raise NotImplementedError

    def iterator(self, prefix=b'', reverse=False):
        '''Return an iterator that yields (key, value) pairs from the
        database sorted by key.

        If `prefix` is set, only keys starting with `prefix` will be
        included.  If `reverse` is True the items are returned in
        reverse order.
        '''
        raise NotImplementedError


class LevelDB(Storage):
    '''LevelDB database engine.'''

    @classmethod
    def import_module(cls):
        import plyvel
        cls.module = plyvel

    def open(self, name, create):
        mof = 512 if self.for_sync else 128
        # Use snappy compression (the default)
        self.db = self.module.DB(name, create_if_missing=create,
                                 max_open_files=mof)
        self.close = self.db.close
        self.get = self.db.get
        self.put = self.db.put
        self.iterator = self.db.iterator
        self.write_batch = partial(self.db.write_batch, transaction=True,
                                   sync=True)


class RocksDB(Storage):
    '''RocksDB database engine (using rocksdict).'''

    @classmethod
    def import_module(cls):
        import rocksdict
        cls.module = rocksdict

    def open(self, name, create):
        mof = 512 if self.for_sync else 128
        if create:
            options = self.module.Options(raw_mode=True)
            options.create_if_missing(True)
            options.set_max_open_files(mof)
            self.db = self.module.Rdict(name, options=options)
        else:
            # Open existing DB without explicit Options so rocksdict
            # auto-detects the comparator and compression from the
            # existing OPTIONS file.
            self.db = self.module.Rdict(name)
        self.get = self.db.get
        self.put = self.db.put

    def close(self):
        self.db.close()
        self.db = self.get = self.put = None

    def write_batch(self):
        return RocksDictWriteBatch(self.db)

    def iterator(self, prefix=b'', reverse=False):
        return RocksDictIterator(self.db, prefix, reverse)


class RocksDictWriteBatch:
    '''A write batch for RocksDB via rocksdict.'''

    def __init__(self, db):
        self.batch = RocksDB.module.WriteBatch(raw_mode=True)
        self.db = db

    def __enter__(self):
        return self.batch

    def __exit__(self, exc_type, exc_val, exc_tb):
        if not exc_val:
            self.db.write(self.batch)


class RocksDictIterator:
    '''An iterator for RocksDB via rocksdict.'''

    def __init__(self, db, prefix, reverse):
        self.prefix = prefix
        self.reverse = reverse
        self.it = db.iter()
        if reverse:
            nxt_prefix = util.increment_byte_string(prefix)
            if nxt_prefix:
                self.it.seek_for_prev(nxt_prefix)
                # If we landed exactly on nxt_prefix, step back
                if self.it.valid() and self.it.key() >= nxt_prefix:
                    self.it.prev()
            else:
                self.it.seek_to_last()
        else:
            self.it.seek(prefix)

    def __iter__(self):
        return self

    def __next__(self):
        if not self.it.valid():
            raise StopIteration
        k, v = self.it.key(), self.it.value()
        if not k.startswith(self.prefix):
            raise StopIteration
        if self.reverse:
            self.it.prev()
        else:
            self.it.next()
        return k, v
