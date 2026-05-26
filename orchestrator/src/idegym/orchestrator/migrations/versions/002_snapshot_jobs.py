"""Add snapshot_prepare_requests, snapshots, and snapshot_jobs tables

Revision ID: 002
Revises: 001
Create Date: 2026-05-21

"""

from pathlib import Path

from alembic import op

revision = "002"
down_revision = "001"
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
    execute_sql_file(migration_dir / "002_up.sql")


def downgrade() -> None:
    migration_dir = Path(__file__).parent
    execute_sql_file(migration_dir / "002_down.sql")
