import re
from collections import Counter
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

ALEMBIC_INI = Path(__file__).resolve().parents[1] / "orchestrator" / "src" / "idegym" / "orchestrator" / "alembic.ini"
VERSIONS = ALEMBIC_INI.parent / "migrations" / "versions"

# script.py.mako emits repr(), so a freshly generated migration is single-quoted
# until ruff format normalizes it.
REVISION_PATTERN = re.compile(r"^revision = [\"']([^\"']+)[\"']", re.MULTILINE)


def script_directory() -> ScriptDirectory:
    return ScriptDirectory.from_config(Config(str(ALEMBIC_INI)))


def declared_revision(path: Path) -> str:
    match = REVISION_PATTERN.search(path.read_text())
    assert match is not None, f"{path.name} declares no revision id"
    return match.group(1)


def version_files() -> list[Path]:
    paths = sorted(path for path in VERSIONS.glob("*.py") if path.name != "__init__.py")
    assert paths, f"no migrations found under {VERSIONS}"
    return paths


def test_revision_ids_are_unique():
    """Two branches creating the same revision id must not both land on main.

    Scans the files rather than the alembic revision map: the map is keyed by
    revision id, so a duplicate is silently collapsed into a single entry.
    """
    revisions = [declared_revision(path) for path in version_files()]
    duplicates = sorted(rev for rev, count in Counter(revisions).items() if count > 1)
    assert duplicates == []


def test_filenames_match_declared_revision():
    for path in version_files():
        assert path.name.startswith(f"{declared_revision(path)}_")


def test_migration_chain_has_a_single_head():
    assert len(script_directory().get_heads()) == 1


def test_every_revision_has_up_and_down_sql():
    for script in script_directory().walk_revisions():
        assert (VERSIONS / f"{script.revision}_up.sql").is_file()
        assert (VERSIONS / f"{script.revision}_down.sql").is_file()
