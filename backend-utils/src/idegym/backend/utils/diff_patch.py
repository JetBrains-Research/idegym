import diff_match_patch as dmp_module


def compute_diff(old: str, new: str) -> str:
    """Computes a patch that makes from `old` string `new` string."""
    dmp = dmp_module.diff_match_patch()
    return dmp.patch_toText(dmp.patch_make(old, new))


def apply_patch(old: str, patch: str) -> str:
    """Applies `patch` obtained in patch_diff to `old` string. Returns new string."""
    dmp = dmp_module.diff_match_patch()
    return dmp.patch_apply(dmp.patch_fromText(patch), old)[0]
