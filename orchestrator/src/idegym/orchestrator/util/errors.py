def format_error(message: str, exception: Exception):
    exception_message = str(exception).strip()
    exception_message = f" ({exception_message})" if exception_message else ""
    return f"{message}: {type(exception).__name__}{exception_message}"
