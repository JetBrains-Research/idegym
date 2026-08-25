from idegym.utils.hashing import md5, sha256


def test_identity():
    assert md5("") == "d41d8cd98f00b204e9800998ecf8427e"
    assert md5("", "", "") == "d41d8cd98f00b204e9800998ecf8427e"


def test_equality():
    assert md5("abc") == md5("a", "b", "c")


def test_order():
    assert md5("abc") != md5("cba")


def test_sha256():
    assert sha256("abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    assert sha256("abc") == sha256("a", "b", "c")
