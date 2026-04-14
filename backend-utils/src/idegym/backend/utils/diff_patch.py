import diff_match_patch as dmp_module


def compute_diff(old: str, new: str) -> str:
    """Return a patch string that transforms `old` into `new`."""
    dmp = dmp_module.diff_match_patch()
    return dmp.patch_toText(dmp.patch_make(old, new))


def apply_patch(old: str, patch: str) -> str:
    """Apply a patch produced by `compute_diff` to `old` and return the result."""
    dmp = dmp_module.diff_match_patch()
    return dmp.patch_apply(dmp.patch_fromText(patch), old)[0]
