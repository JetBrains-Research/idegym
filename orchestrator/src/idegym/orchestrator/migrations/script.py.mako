"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from pathlib import Path
import re
${imports if imports else ""}

# revision identifiers, used by Alembic.
revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}


def execute_sql_file(sql_file_path: Path) -> None:
    """Execute SQL statements from a file, splitting them properly."""
    with open(sql_file_path, 'r') as f:
        sql_content = f.read()

    if not sql_content.strip():
        return

    # Split SQL content into individual statements
    # Remove comments and empty lines first
    lines = []
    for line in sql_content.split('\n'):
        line = line.strip()
        if line and not line.startswith('--'):
            lines.append(line)

    # Join lines and split by semicolon
    clean_sql = ' '.join(lines)
    statements = [stmt.strip() for stmt in clean_sql.split(';') if stmt.strip()]

    # Execute each statement individually
    for statement in statements:
        if statement:
            op.execute(statement)


def upgrade() -> None:
    # Get the directory where this migration file is located
    migration_dir = Path(__file__).parent
    sql_file_path = migration_dir / '${up_revision}_up.sql'
    execute_sql_file(sql_file_path)


def downgrade() -> None:
    # Get the directory where this migration file is located
    migration_dir = Path(__file__).parent
    # Use current (up) revision for the down SQL file as well
    sql_file_path = migration_dir / '${up_revision}_down.sql'
    execute_sql_file(sql_file_path)
