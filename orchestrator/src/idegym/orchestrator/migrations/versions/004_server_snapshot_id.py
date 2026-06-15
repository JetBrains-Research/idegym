"""Add snapshot_id column to servers

Revision ID: 004
Revises: 003
Create Date: 2026-06-16

"""

from pathlib import Path

from alembic import op

revision = "004"
down_revision = "003"
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
    execute_sql_file(migration_dir / "004_up.sql")


def downgrade() -> None:
    migration_dir = Path(__file__).parent
    execute_sql_file(migration_dir / "004_down.sql")
