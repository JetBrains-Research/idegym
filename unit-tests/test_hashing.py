from idegym.utils.hashing import md5


def test_identity():
    assert md5("") == "d41d8cd98f00b204e9800998ecf8427e"
    assert md5("", "", "") == "d41d8cd98f00b204e9800998ecf8427e"


def test_equality():
    assert md5("abc") == md5("a", "b", "c")


def test_order():
    assert md5("abc") != md5("cba")
