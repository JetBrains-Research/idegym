from structlog import dev


class LevelFormatter(dev.LogLevelColumnFormatter):
    def __init__(self):
        super().__init__(
            level_styles=dict(),
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
        )
