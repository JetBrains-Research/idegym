import hashlib


def md5(value: str, /, *other: str) -> str:
    """Compute the MD5 hash of the given value(s)."""
    digest = hashlib.md5()
    for value in (value, *other):
        digest.update(value.encode())
    return digest.hexdigest()
