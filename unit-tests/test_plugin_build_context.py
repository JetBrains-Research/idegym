"""Unit tests for staging plugin build assets into the Docker/Kaniko build context.

Covers the mechanism that lets plugin authors build idea/pycharm images without a checkout
of the idegym repo: assets ship in the wheel and are resolved by ``plugin_asset()``, declared
by each plugin's ``get_context_files()``, collected onto ``ImageBuildSpec.context_files``,
staged in place into the caller's build context (and cleaned up) by the local build driver, and
resolved via a git checkout by the Kaniko path.
"""

import re
from contextlib import ExitStack

import pytest
from idegym.api.image_build import ImageBuildSpec
from idegym.image.builder import Image
from idegym.image.docker_service import DockerService
from idegym.plugins.defaults.image import Project
from idegym.plugins.idea.image import Idea
from idegym.plugins.plugin_utils import plugin_asset
from idegym.plugins.pycharm.image import PyCharm

pytestmark = pytest.mark.unit

_COPY_RE = re.compile(r"^COPY\s+(?!--from)(?:--\S+\s+)*(\S+)\s", re.MULTILINE)


def _copy_sources(dockerfile: str) -> set[str]:
    """Local (non ``--from``) COPY source paths in a rendered Dockerfile."""
    return set(_COPY_RE.findall(dockerfile))


# --- plugin_asset -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "package, parts",
    [
        ("idegym.plugins.idea", ("scripts", "start-idea.sh")),
        ("idegym.plugins.idea", ("scripts", "check-mcp.sh")),
        ("idegym.plugins.idea", ("scripts", "supervisord-idea.conf")),
        ("idegym.plugins.idea", ("project-opener", "project-opener.zip")),
        ("idegym.plugins.pycharm", ("scripts", "start-pycharm.sh")),
        ("idegym.plugins.pycharm", ("project-opener", "project-opener.zip")),
    ],
)
def test_plugin_asset_resolves_to_a_real_nonempty_file(package, parts):
    asset = plugin_asset(package, *parts)
    assert asset.is_file()
    assert len(asset.read_bytes()) > 0


# --- get_context_files mirrors the COPY directives --------------------------------------


def _ctx_with_project():
    from idegym.api.plugin import BuildContext

    return BuildContext(base="debian:bookworm-slim").with_extra("idegym.has_project", True)


def _ctx_without_project():
    from idegym.api.plugin import BuildContext

    return BuildContext(base="debian:bookworm-slim")


@pytest.mark.parametrize(
    "plugin, ctx_factory",
    [
        (Idea(), _ctx_with_project),  # open-project: zip + scripts
        (Idea(mcp_steroid=True), _ctx_without_project),  # mcp-steroid start: scripts only
        (Idea(open_project=False), _ctx_without_project),  # neither: nothing
        (Idea(open_project=False), _ctx_with_project),  # has project but opt-out: nothing
        (PyCharm(), _ctx_with_project),
        (PyCharm(mcp_steroid=True), _ctx_without_project),
        (PyCharm(open_project=False), _ctx_without_project),
    ],
)
def test_context_files_keys_match_copy_directives(plugin, ctx_factory):
    ctx = ctx_factory()
    assert set(plugin.get_context_files(ctx)) == _copy_sources(plugin.render(ctx))


# --- to_spec collects the bytes --------------------------------------------------------


def test_to_spec_collects_asset_bytes_for_idea():
    spec = Image.from_base("debian:bookworm-slim").with_plugin(Idea(mcp_steroid=True)).to_spec()
    assert set(spec.context_files) == {
        "plugins/idea/scripts/check-mcp.sh",
        "plugins/idea/scripts/start-idea.sh",
        "plugins/idea/scripts/supervisord-idea.conf",
    }
    assert all(len(data) > 0 for data in spec.context_files.values())


def test_to_spec_open_project_includes_zip():
    spec = Image.from_base("debian:bookworm-slim").with_plugin(Project.from_local("proj")).with_plugin(Idea()).to_spec()
    assert "plugins/idea/project-opener/project-opener.zip" in spec.context_files
    assert len(spec.context_files["plugins/idea/project-opener/project-opener.zip"]) > 0


def test_to_spec_without_ide_plugin_has_no_context_files():
    spec = Image.from_base("debian:bookworm-slim").run_commands("echo hi").to_spec()
    assert spec.context_files == {}


# --- ImageBuildSpec.context_files: excluded from serialization, hashed into the version --


def test_context_files_excluded_from_serialization():
    spec = ImageBuildSpec(dockerfile_content="FROM x", context_files={"a/b.sh": b"data"})
    assert "context_files" not in spec.model_dump()
    assert spec.context_files == {"a/b.sh": b"data"}  # still accessible


def test_image_version_changes_when_asset_content_changes():
    a = ImageBuildSpec(dockerfile_content="FROM x", context_files={"a/b.sh": b"v1"})
    b = ImageBuildSpec(dockerfile_content="FROM x", context_files={"a/b.sh": b"v2"})
    same = ImageBuildSpec(dockerfile_content="FROM x", context_files={"a/b.sh": b"v1"})
    assert a.image_version() != b.image_version()
    assert a.image_version() == same.image_version()


# --- DockerService._stage_context_files (stages in place, cleans up on exit) -----------


def test_stage_context_files_noop_when_no_files(tmp_path):
    with ExitStack() as stack:
        DockerService._stage_context_files(str(tmp_path), {}, stack)
    assert list(tmp_path.iterdir()) == []


def test_stage_context_files_leaves_present_assets_untouched(tmp_path):
    existing = tmp_path / "plugins" / "idea" / "scripts" / "x.sh"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"present")
    with ExitStack() as stack:
        DockerService._stage_context_files(str(tmp_path), {"plugins/idea/scripts/x.sh": b"other"}, stack)
        assert existing.read_bytes() == b"present"  # never clobbered
    assert existing.read_bytes() == b"present"  # and not removed on cleanup


def test_stage_context_files_writes_missing_assets_in_place_and_preserves_siblings(tmp_path):
    # A plugin author's project dir: has its own COPY source, but not the plugin assets.
    project_file = tmp_path / "app.py"
    project_file.write_text("print('hi')")
    files = {"plugins/idea/scripts/x.sh": b"asset", "plugins/idea/project-opener/o.zip": b"zip"}
    with ExitStack() as stack:
        DockerService._stage_context_files(str(tmp_path), files, stack)
        # assets available in the caller's own context, alongside its files
        assert (tmp_path / "plugins" / "idea" / "scripts" / "x.sh").read_bytes() == b"asset"
        assert (tmp_path / "plugins" / "idea" / "project-opener" / "o.zip").read_bytes() == b"zip"
        assert project_file.read_text() == "print('hi')"
    # staged files and the dirs they created are gone; the caller's own file remains
    assert not (tmp_path / "plugins").exists()
    assert project_file.read_text() == "print('hi')"


def test_stage_context_files_preserves_preexisting_parent_dirs(tmp_path):
    # A pre-existing `plugins/` dir must survive cleanup — only newly-created dirs are pruned.
    (tmp_path / "plugins" / "keep.txt").parent.mkdir(parents=True)
    (tmp_path / "plugins" / "keep.txt").write_text("keep")
    with ExitStack() as stack:
        DockerService._stage_context_files(str(tmp_path), {"plugins/idea/scripts/x.sh": b"asset"}, stack)
    assert not (tmp_path / "plugins" / "idea").exists()  # created subtree pruned
    assert (tmp_path / "plugins" / "keep.txt").read_text() == "keep"  # pre-existing dir kept


@pytest.mark.parametrize("dest", ["/etc/passwd", "../escape.sh", "plugins/../../escape.sh"])
def test_stage_context_files_rejects_paths_escaping_the_context(tmp_path, dest):
    # A destination must stay inside the build context: absolute paths or `..` are rejected
    # before any write, so a plugin cannot clobber files outside the caller's context.
    with ExitStack() as stack:
        with pytest.raises(ValueError, match="relative path within the build context"):
            DockerService._stage_context_files(str(tmp_path), {dest: b"asset"}, stack)
    assert list(tmp_path.iterdir()) == []  # nothing written
