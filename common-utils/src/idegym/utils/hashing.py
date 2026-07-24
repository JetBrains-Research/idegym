import hashlib


def md5(value: str, /, *other: str) -> str:
    """Compute the MD5 hash of the given value(s)."""
    digest = hashlib.md5()
    for item in (value, *other):
        digest.update(item.encode())
    return digest.hexdigest()
