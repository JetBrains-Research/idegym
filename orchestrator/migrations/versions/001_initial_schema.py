"""Initial schema

Revision ID: 001
Revises:
Create Date: 2026-03-13 16:08:07.350514

"""

from pathlib import Path

from alembic import op

# revision identifiers, used by Alembic.
revision = "001"
down_revision = None
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
    execute_sql_file(migration_dir / "001_up.sql")


def downgrade() -> None:
    migration_dir = Path(__file__).parent
    execute_sql_file(migration_dir / "001_down.sql")
