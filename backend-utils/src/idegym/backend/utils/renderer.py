from structlog import dev
from structlog.typing import ExceptionRenderer as ExceptionFormatter


def _exception_formatter() -> ExceptionFormatter:
    """structlog's default console formatter, minus the frame locals it prints by default.

    The locals of a frame holding the ``Config`` include the database password, so they must
    never reach a log line (see ``idegym.backend.utils.logging``).
    """
    if dev.rich is not None:
        return dev.RichTracebackFormatter(show_locals=False)
    return dev.plain_traceback


class LevelFormatter(dev.LogLevelColumnFormatter):
    def __init__(self):
        super().__init__(
            level_styles={},
            reset_style="",
            width=0,
        )


class DelimitedValueFormatter(dev.KeyValueColumnFormatter):
    def __init__(self):
        super().__init__(
            key_style=None,
            value_style="",
            reset_style="",
            value_repr=str,
            prefix="[",
            postfix="]",
        )


class ValueFormatter(dev.KeyValueColumnFormatter):
    def __init__(self):
        super().__init__(
            key_style=None,
            value_style="",
            reset_style="",
            value_repr=str,
        )


class KeyValueFormatter(dev.KeyValueColumnFormatter):
    def __init__(self):
        super().__init__(
            key_style="",
            value_style="",
            reset_style="",
            value_repr=str,
        )


class ConsoleRenderer(dev.ConsoleRenderer):
    def __init__(self):
        value_formatter = ValueFormatter()
        level_formatter = LevelFormatter()
        delimited_value_formatter = DelimitedValueFormatter()
        key_value_formatter = KeyValueFormatter()
        super().__init__(
            columns=[
                dev.Column("timestamp", value_formatter),
                dev.Column("pid", delimited_value_formatter),
                dev.Column("level", level_formatter),
                dev.Column("logger", delimited_value_formatter),
                dev.Column("event", value_formatter),
                dev.Column("", key_value_formatter),
            ],
            exception_formatter=_exception_formatter(),
        )
