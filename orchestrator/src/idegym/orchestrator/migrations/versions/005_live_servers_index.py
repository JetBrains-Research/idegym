"""Add a partial index over live servers

The watcher's cleanup, crash detection and quota recount select servers with
availability in (ALIVE, FINISHED, REUSED); the index keeps those scans off
the terminal rows that make up most of the table.

Revision ID: 005
Revises: 004
Create Date: 2026-09-22

"""

from pathlib import Path

from alembic import op

revision = "005"
down_revision = "004"
branch_labels = None
depends_on = None


def execute_sql_file(sql_file_path: Path) -> None:
    with open(sql_file_path, "r") as f:
        sql_content = f.read()

    if not sql_content.strip():
        return

    lines = []
    for line in sql_content.split("\n"):
        line = line.strip()
        if line and not line.startswith("--"):
            lines.append(line)

    clean_sql = " ".join(lines)
    statements = [stmt.strip() for stmt in clean_sql.split(";") if stmt.strip()]

    for statement in statements:
        if statement:
            op.execute(statement)


def upgrade() -> None:
    migration_dir = Path(__file__).parent
    execute_sql_file(migration_dir / "005_up.sql")


def downgrade() -> None:
    migration_dir = Path(__file__).parent
    execute_sql_file(migration_dir / "005_down.sql")
