"""Index stale in-progress operations (revision 007, after 006)."""

from pathlib import Path

from alembic import op
from sqlalchemy import text

revision = "007"
down_revision = "006"
branch_labels = None
depends_on = None


def _index_ready() -> bool:
    return bool(
        op.get_bind().scalar(
            text(
                "SELECT indisvalid AND indisready AND indislive FROM pg_index "
                "WHERE indexrelid = to_regclass('public.ix_async_operations_in_progress_started')"
            )
        )
    )


def _execute_sql(suffix: str) -> None:
    for statement in Path(__file__).with_name(f"007_{suffix}.sql").read_text().split(";"):
        if statement.strip():
            op.execute(statement)


def upgrade() -> None:
    context = op.get_context()
    with context.autocommit_block():
        if not context.as_sql:
            populated = op.get_bind().scalar(text("SELECT EXISTS (SELECT 1 FROM public.async_operations LIMIT 1)"))
            if populated and not _index_ready():
                raise RuntimeError(
                    "Apply 007_up.sql with psql in autocommit mode before upgrading this populated database"
                )
        _execute_sql("up")
        if not context.as_sql and not _index_ready():
            raise RuntimeError("Stale-operation index is invalid; repair it before upgrading")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        _execute_sql("down")
