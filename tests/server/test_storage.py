import pytest
import os

from electrumx.server.storage import Storage, db_class
from electrumx.lib.util import subclasses

# Find out which db engines to test
# Those that are not installed will be skipped
db_engines = []
for c in subclasses(Storage):
    try:
        c.import_module()
    except ImportError:
        db_engines.append(pytest.param(c.__name__, marks=pytest.mark.skip))
    else:
        db_engines.append(c.__name__)


@pytest.fixture(params=db_engines)
def db(tmpdir, request):
    cwd = os.getcwd()
    os.chdir(str(tmpdir))
    db = db_class(request.param)("db", False)
    yield db
    os.chdir(cwd)
    db.close()


def test_put_get(db):
    db.put(b"x", b"y")
    assert db.get(b"x") == b"y"


def test_batch(db):
    db.put(b"a", b"1")
    with db.write_batch() as b:
        b.put(b"a", b"2")
        assert db.get(b"a") == b"1"
    assert db.get(b"a") == b"2"


def test_iterator(db):
    """
    The iterator should contain all key/value pairs starting with prefix
    ordered by key.
    """
    for i in range(5):
        db.put(b"abc" + str.encode(str(i)), str.encode(str(i)))
    db.put(b"abc", b"")
    db.put(b"a", b"xyz")
    db.put(b"abd", b"x")
    assert list(db.iterator(prefix=b"abc")) == [(b"abc", b"")] + [
            (b"abc" + str.encode(str(i)), str.encode(str(i))) for
            i in range(5)
        ]


def test_iterator_reverse(db):
    for i in range(5):
        db.put(b"abc" + str.encode(str(i)), str.encode(str(i)))
    db.put(b"a", b"xyz")
    db.put(b"abd", b"x")
    assert list(db.iterator(prefix=b"abc", reverse=True)) == [
            (b"abc" + str.encode(str(i)), str.encode(str(i))) for
            i in reversed(range(5))
        ]


def test_iterator_seek(db):
    db.put(b"first-key1", b"val")
    db.put(b"first-key2", b"val")
    db.put(b"first-key3", b"val")
    db.put(b"key-1", b"value-1")
    db.put(b"key-5", b"value-5")
    db.put(b"key-3", b"value-3")
    db.put(b"key-8", b"value-8")
    db.put(b"key-2", b"value-2")
    db.put(b"key-4", b"value-4")
    db.put(b"last-key1", b"val")
    db.put(b"last-key2", b"val")
    db.put(b"last-key3", b"val")
    # forward-iterate, key present, no prefix
    it = db.iterator()
    it.seek(b"key-4")
    assert list(it) == [(b"key-4", b"value-4"), (b"key-5", b"value-5"), (b"key-8", b"value-8"),
                        (b"last-key1", b"val"), (b"last-key2", b"val"), (b"last-key3", b"val")]
    # forward-iterate, key present
    it = db.iterator(prefix=b"key-")
    it.seek(b"key-4")
    assert list(it) == [(b"key-4", b"value-4"), (b"key-5", b"value-5"),
                        (b"key-8", b"value-8")]
    # forward-iterate, key missing
    it = db.iterator(prefix=b"key-")
    it.seek(b"key-6")
    assert list(it) == [(b"key-8", b"value-8")]
    # forward-iterate, after last prefix
    it = db.iterator(prefix=b"key-")
    it.seek(b"key-9")
    assert list(it) == []
    # forward-iterate, after last, no prefix
    it = db.iterator()
    it.seek(b"z")
    assert list(it) == []
    # forward-iterate, no such prefix
    it = db.iterator(prefix=b"key---")
    it.seek(b"key---5")
    assert list(it) == []
    # forward-iterate, seek outside prefix
    it = db.iterator(prefix=b"key-")
    it.seek(b"last-key2")
    assert list(it) == []
    # reverse-iterate, key present
    it = db.iterator(prefix=b"key-", reverse=True)
    it.seek(b"key-4")
    assert list(it) == [(b"key-3", b"value-3"), (b"key-2", b"value-2"), (b"key-1", b"value-1")]
    # reverse-iterate, key missing
    it = db.iterator(prefix=b"key-", reverse=True)
    it.seek(b"key-7")
    assert list(it) == [(b"key-5", b"value-5"), (b"key-4", b"value-4"), (b"key-3", b"value-3"),
                        (b"key-2", b"value-2"), (b"key-1", b"value-1")]
    # reverse-iterate, before first prefix
    it = db.iterator(prefix=b"key-", reverse=True)
    it.seek(b"key-0")
    assert list(it) == []
    # reverse-iterate, before first, no prefix
    it = db.iterator(reverse=True)
    it.seek(b"a")
    assert list(it) == []
    # reverse-iterate, no such prefix
    it = db.iterator(prefix=b"key---", reverse=True)
    it.seek(b"key---5")
    assert list(it) == []
    # reverse-iterate, seek outside prefix
    it = db.iterator(prefix=b"key-", reverse=True)
    it.seek(b"first-key2")
    assert list(it) == []


def test_close(db):
    db.put(b"a", b"b")
    db.close()
    db = db_class(db.__class__.__name__)("db", False)
    assert db.get(b"a") == b"b"
