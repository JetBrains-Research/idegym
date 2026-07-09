from idegym.utils.dict import deep_merge, walk


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


def test_deep_merge_overrides_scalars_and_adds_keys():
    base = {"a": 1, "b": 2}
    override = {"b": 3, "c": 4}
    assert deep_merge(base, override) == {"a": 1, "b": 3, "c": 4}


def test_deep_merge_recurses_into_nested_dicts():
    base = {"meta": {"name": "x", "labels": {"app": "srv"}}}
    override = {"meta": {"labels": {"tier": "agent"}}}
    assert deep_merge(base, override) == {"meta": {"name": "x", "labels": {"app": "srv", "tier": "agent"}}}


def test_deep_merge_replaces_lists_by_default():
    base = {"items": [1, 2]}
    override = {"items": [3]}
    assert deep_merge(base, override) == {"items": [3]}


def test_deep_merge_concatenates_lists_when_requested():
    base = {"items": [1, 2]}
    override = {"items": [3]}
    assert deep_merge(base, override, concat_lists=True) == {"items": [1, 2, 3]}


def test_deep_merge_does_not_mutate_inputs():
    base = {"a": {"b": 1}, "list": [1]}
    override = {"a": {"c": 2}, "list": [2]}
    deep_merge(base, override, concat_lists=True)
    assert base == {"a": {"b": 1}, "list": [1]}
    assert override == {"a": {"c": 2}, "list": [2]}
