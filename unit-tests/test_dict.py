from idegym.utils.dict import walk


def test_walk_with_empty_dictionary():
    dictionary = {}
    expected = list(dictionary.values())
    actual = list(walk(dictionary))
    assert expected == actual


def test_walk_with_flat_dictionary():
    dictionary = {
        "key1": "value1",
        "key2": "value2",
        "key3": "value3",
    }
    expected = list(dictionary.values())
    actual = list(walk(dictionary))
    assert expected == actual


def test_walk_with_nested_dictionary():
    dictionary = {
        "key1": "value1",
        "key2": {
            "subkey1": "subvalue1",
            "subkey2": "subvalue2",
        },
        "key3": "value3",
    }
    expected = ["value1", "subvalue1", "subvalue2", "value3"]
    actual = list(walk(dictionary))
    assert expected == actual


def test_walk_with_deeply_nested_dictionary():
    dictionary = {
        "key1": {
            "key2": {
                "key3": {
                    "key4": "value1",
                },
            },
        },
        "key5": "value2",
    }
    expected = ["value1", "value2"]
    actual = list(walk(dictionary))
    assert expected == actual


def test_walk_multiple_value_types():
    dictionary = {
        "key1": 1,
        "key2": {
            "key3": 3.14,
            "key4": ["a", "b"],
        },
        "key5": True,
    }
    expected = [1, 3.14, ["a", "b"], True]
    actual = list(walk(dictionary))
    assert expected == actual
