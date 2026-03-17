from os import environ as env
from os import pathsep
from pathlib import Path
from tempfile import gettempdir

from idegym.backend.utils.environment import IDEGYM_PATH, PYTHONPATH, cleanenv
from pytest import fail, mark, param
from pytest_mock import MockerFixture

tempdir = Path(gettempdir())
target = tempdir / "opt" / "idegym"
other = tempdir / "other" / "path"


def test_empty_environment(mocker: MockerFixture, tmp_path: Path):
    mocker.patch.object(
        target=env,
        attribute=f"{env.copy.__name__}",
        side_effect=lambda: {},
    )
    assert env.copy() == cleanenv()


def test_already_clean_environment(mocker: MockerFixture):
    mocker.patch.object(
        target=env,
        attribute=f"{env.copy.__name__}",
        side_effect=lambda: {
            PYTHONPATH: str(tempdir / "idegym" / "django"),
            "TZ": "UTC",
        },
    )
    assert env.copy() == cleanenv(), "Environment should not be modified!"


def test_variable_removal(mocker: MockerFixture):
    mocker.patch.object(
        target=env,
        attribute=f"{env.copy.__name__}",
        side_effect=lambda: {
            IDEGYM_PATH: str(target),
            PYTHONPATH: str(target),
            "TZ": "UTC",
        },
    )
    if clean := cleanenv():
        assert IDEGYM_PATH not in clean, "Variable has not been removed!"
        assert PYTHONPATH not in clean, "Empty variable has not been removed!"
    else:
        fail("Environment should not be empty!")


@mark.parametrize(
    "paths",
    [
        param((str(target), str(other)), id="prefix"),
        param((str(other), str(target)), id="suffix"),
    ],
)
def test_single_removal(mocker: MockerFixture, paths: tuple[str, ...]):
    mocker.patch.object(
        target=env,
        attribute=f"{env.copy.__name__}",
        side_effect=lambda: {
            PYTHONPATH: pathsep.join(paths),
            IDEGYM_PATH: str(target),
        },
    )
    current = env.copy()
    clean = cleanenv()
    assert current[PYTHONPATH] != clean[PYTHONPATH], "Environments should not be equal!"
    assert IDEGYM_PATH not in clean, "Variable has not been removed!"
    assert PYTHONPATH in clean, "Non-empty variable has been removed!"
    assert clean[PYTHONPATH] == str(other), "Incorrect value of modified variable!"


@mark.parametrize(
    "paths",
    [
        param((str(other), str(target), str(target), str(other)), id="inside"),
        param((str(target), str(other), str(other), str(target)), id="around"),
        param((str(target), str(other), str(target), str(other)), id="mixed"),
    ],
)
def test_multiple_removal(mocker: MockerFixture, paths: tuple[str, ...]):
    mocker.patch.object(
        target=env,
        attribute=f"{env.copy.__name__}",
        side_effect=lambda: {
            PYTHONPATH: pathsep.join(paths),
            IDEGYM_PATH: str(target),
        },
    )
    current = env.copy()
    clean = cleanenv()
    paths = (str(other), str(other))
    assert current[PYTHONPATH] != clean[PYTHONPATH], "Environments should not be equal!"
    assert IDEGYM_PATH not in clean, "Variable has not been removed!"
    assert PYTHONPATH in clean, "Non-empty variable has been removed!"
    assert clean[PYTHONPATH] == pathsep.join(paths), "Incorrect value of modified variable!"
