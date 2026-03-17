from idegym.backend.utils.diff_patch import apply_patch, compute_diff
from pytest import mark, param


@mark.parametrize(
    "str1,str2",
    [
        param("", "abc", id="empty1"),
        param("", "abc\n", id="empty2"),
        param("", "abc\ndef", id="empty3"),
        param("abc\ndef", "abcdef", id="newline1"),
        param("abc\ndef\n", "abc\ndef", id="newline2"),
        param("\nabc\ndef", "abc\ndef", id="newline3"),
        param("abc", "def", id="simple1"),
        param("abc\ndef", "abc", id="simple2"),
        param("abc\ndef", "abc\n", id="simple3"),
        param("abc\ndef", "\ndef", id="simple4"),
        param("abc\ndef", "def", id="simple5"),
        param("abc\ndef", "a11\ndef", id="simple6"),
    ],
)
def test_diff_patch(str1: str, str2: str):
    def do_test(old: str, new: str):
        patch = compute_diff(old, new)
        assert patch is not None
        new_from_patch = apply_patch(old, patch)
        assert new_from_patch == new, f"old={old!r}, patch={patch!r}, result={new_from_patch!r}, expected={new!r}"

    do_test(str1, str2)
    do_test(str2, str1)
