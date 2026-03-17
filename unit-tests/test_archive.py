from idegym.api.download import ArchiveDescriptor
from pydantic import ValidationError
from pytest import fixture, mark, param, raises


@fixture
def url() -> str:
    return "https://example.com"


@mark.parametrize(
    "name",
    [
        param("project.zip", id="zip"),
        param(".project.zip", id="dot-zip"),
        param("project.tar.gz", id="tar-gz"),
        param(".project.tar.gz", id="dot-tar-gz"),
    ],
)
def test_archive_name_valid(name: str, url: str):
    ArchiveDescriptor(name=name, url=url)


@mark.parametrize(
    "name",
    [
        param(None, id="none"),
        param("", id="empty"),
        param(" ", id="blank"),
        param(".zip", id="no-name"),
        param("project", id="no-extension"),
        param("project.xz", id="unknown-format"),
    ],
)
def test_archive_name_invalid(name: str, url: str):
    with raises(ValidationError):
        ArchiveDescriptor(name=name, url=url)
