import subprocess
import sys
from textwrap import dedent

from idegym.api.plugin import BuildContext, PluginBase, get_all_server_plugins, image_plugin, server_plugin
from idegym.image.builder import Image
from idegym.image.serialization import deserialize_plugin, serialize_plugin
from idegym.plugins.defaults.image import BaseSystem, IdeGYMServer, MCPUpstream, Permissions, Project, User
from idegym.plugins.idea.image import Idea
from idegym.plugins.pycharm.image import PyCharm
from pytest import mark, param, raises

# ---------------------------------------------------------------------------
# BuildContext
# ---------------------------------------------------------------------------


def test_build_context_defaults():
    ctx = BuildContext(base="debian:bookworm-slim")
    assert ctx.current_user == "root"
    assert ctx.home == "/root"
    assert ctx.project_root == "/root/work"
    assert ctx.request is None
    assert ctx.labels == {}
    assert ctx.context_path == "."
    assert ctx.extras == {}


def test_build_context_updated():
    ctx = BuildContext(base="debian:bookworm-slim")
    updated = ctx.updated(current_user="appuser")
    assert updated.current_user == "appuser"
    assert ctx.current_user == "root"  # Original unchanged


def test_build_context_extras():
    ctx = BuildContext(base="debian:bookworm-slim")
    ctx2 = ctx.with_extra("my.key", 42)
    assert ctx2.get_extra("my.key") == 42
    assert ctx.get_extra("my.key") is None  # Original unchanged


def test_build_context_require_extra_missing():
    ctx = BuildContext(base="debian:bookworm-slim")
    with raises(KeyError, match="Missing build context extra"):
        ctx.require_extra("non.existent")


# ---------------------------------------------------------------------------
# PluginBase defaults
# ---------------------------------------------------------------------------


def test_plugin_base_apply_is_noop():
    plugin = PluginBase()
    ctx = BuildContext(base="debian:bookworm-slim")
    assert plugin.apply(ctx) is ctx


def test_plugin_base_render_returns_empty():
    plugin = PluginBase()
    ctx = BuildContext(base="debian:bookworm-slim")
    assert plugin.render(ctx) == ""


# ---------------------------------------------------------------------------
# BaseSystem plugin
# ---------------------------------------------------------------------------


def test_base_system_render_contains_apt_get():
    plugin = BaseSystem()
    ctx = BuildContext(base="debian:bookworm-slim")
    fragment = plugin.render(ctx)
    assert "apt-get install" in fragment
    assert "apt-get clean" in fragment
    assert "update-ca-certificates" in fragment


def test_base_system_default_packages():
    plugin = BaseSystem()
    fragment = plugin.render(BuildContext(base="debian:bookworm-slim"))
    for pkg in ("bash", "curl", "git", "sudo"):
        assert pkg in fragment


def test_base_system_custom_packages():
    plugin = BaseSystem(packages=("curl", "jq"))
    fragment = plugin.render(BuildContext(base="debian:bookworm-slim"))
    assert "curl" in fragment
    assert "jq" in fragment
    assert "bash" not in fragment  # Not in custom list


def test_base_system_empty_packages_renders_empty():
    plugin = BaseSystem(packages=())
    fragment = plugin.render(BuildContext(base="debian:bookworm-slim"))
    assert fragment == ""


# ---------------------------------------------------------------------------
# User plugin
# ---------------------------------------------------------------------------


def test_user_apply_updates_context():
    plugin = User(username="devuser", uid=2000, gid=2000, home="/home/devuser")
    ctx = BuildContext(base="debian:bookworm-slim")
    updated = plugin.apply(ctx)
    assert updated.current_user == "devuser"
    assert updated.home == "/home/devuser"


def test_user_apply_defaults_home():
    plugin = User(username="testuser")
    ctx = BuildContext(base="debian:bookworm-slim")
    updated = plugin.apply(ctx)
    assert updated.home == "/home/testuser"


def test_user_render_creates_user():
    plugin = User(username="appuser", uid=1000, gid=1000)
    ctx = BuildContext(base="debian:bookworm-slim")
    ctx = plugin.apply(ctx)
    fragment = plugin.render(ctx)
    assert "useradd" in fragment or "usermod" in fragment
    assert "appuser" in fragment


def test_user_render_with_sudo():
    plugin = User(username="appuser", sudo=True)
    ctx = plugin.apply(BuildContext(base="debian:bookworm-slim"))
    fragment = plugin.render(ctx)
    assert "sudoers" in fragment
    assert "NOPASSWD" in fragment


def test_user_render_without_sudo():
    plugin = User(username="appuser", sudo=False)
    ctx = plugin.apply(BuildContext(base="debian:bookworm-slim"))
    fragment = plugin.render(ctx)
    assert "NOPASSWD" not in fragment


# ---------------------------------------------------------------------------
# Project plugin
# ---------------------------------------------------------------------------


def test_project_from_git():
    plugin = Project.from_git(url="https://github.com/django/django.git", ref="stable/4.2.x")
    assert plugin.source == "git"
    assert plugin.url == "https://github.com/django/django.git"
    assert plugin.ref == "stable/4.2.x"


def test_project_from_resource():
    plugin = Project.from_resource(
        url="https://github.com/octocat/hello-world.git",
        ref="HEAD",
        path="README.md",
    )
    assert plugin.source == "resource"
    assert plugin.path == "README.md"


def test_project_apply_sets_request():
    plugin = Project.from_git(url="https://github.com/realpython/python-scripts.git", ref="HEAD")
    ctx = BuildContext(base="debian:bookworm-slim")
    updated = plugin.apply(ctx)
    assert updated.request is not None
    assert "python-scripts" in updated.request.descriptor.name
    assert "idegym.repository.url" in updated.labels


def test_project_apply_only_one_allowed():
    plugin = Project.from_git(url="https://github.com/realpython/python-scripts.git", ref="HEAD")
    ctx = BuildContext(base="debian:bookworm-slim")
    ctx = plugin.apply(ctx)
    with raises(ValueError, match="Only one Project plugin"):
        plugin.apply(ctx)


def test_project_render_contains_download_commands():
    plugin = Project.from_git(url="https://github.com/realpython/python-scripts.git", ref="HEAD")
    ctx = BuildContext(base="debian:bookworm-slim")
    ctx = plugin.apply(ctx)
    fragment = plugin.render(ctx)
    assert "download" in fragment
    assert "extract" in fragment
    assert "IDEGYM_PROJECT_ARCHIVE_URL" in fragment


# ---------------------------------------------------------------------------
# Permissions plugin
# ---------------------------------------------------------------------------


def test_permissions_render_chown():
    plugin = Permissions(paths={"/app": {"owner": "appuser", "group": "appuser"}})
    ctx = BuildContext(base="debian:bookworm-slim")
    fragment = plugin.render(ctx)
    assert "chown" in fragment
    assert "/app" in fragment


def test_permissions_render_chmod():
    plugin = Permissions(paths={"/app": {"owner": "appuser", "mode": "755"}})
    ctx = BuildContext(base="debian:bookworm-slim")
    fragment = plugin.render(ctx)
    assert "chmod" in fragment


def test_permissions_empty_paths_renders_empty():
    plugin = Permissions(paths={})
    fragment = plugin.render(BuildContext(base="debian:bookworm-slim"))
    assert fragment == ""


# ---------------------------------------------------------------------------
# Image builder
# ---------------------------------------------------------------------------


def test_image_from_base():
    image = Image.from_base("debian:bookworm-slim")
    assert image.base == "debian:bookworm-slim"
    assert image.name is None
    assert image.plugins == ()
    assert image.commands == ()


def test_image_named():
    image = Image.from_base("debian:bookworm-slim").named("my-image")
    assert image.name == "my-image"


@mark.parametrize(
    "name",
    [
        param("my-image", id="simple"),
        param("my-image:latest", id="with-tag"),
        param("registry.example.com/org/image:v1.2.3", id="full-reference"),
        param("image@sha256:abcdef1234567890", id="digest"),
    ],
)
def test_image_named_accepts_valid_oci_name(name):
    image = Image.from_base("debian:bookworm-slim").named(name)
    assert image.name == name


@mark.parametrize(
    "name",
    [
        param("MyImage", id="uppercase"),
        param("my image", id="space"),
        param("image:bad?tag", id="illegal-char"),
        param("", id="empty"),
    ],
)
def test_image_named_rejects_invalid_oci_name(name):
    with raises(ValueError):
        Image.from_base("debian:bookworm-slim").named(name)


def test_image_with_plugin():
    plugin = BaseSystem()
    image = Image.from_base("debian:bookworm-slim").with_plugin(plugin)
    assert len(image.plugins) == 1
    assert image.plugins[0] == plugin


def test_image_run_commands():
    image = Image.from_base("debian:bookworm-slim").run_commands("echo hello", "ls /")
    assert image.commands == ("echo hello", "ls /")


def test_image_run_commands_empty_noop():
    image = Image.from_base("debian:bookworm-slim")
    assert image.run_commands() is image


def test_image_to_spec_basic():
    image = Image.from_base("debian:bookworm-slim")
    spec = image.to_spec()
    assert spec.dockerfile_content.startswith("FROM debian:bookworm-slim")
    assert spec.request is None
    assert spec.labels == {}


def test_image_to_spec_with_commands():
    image = Image.from_base("debian:bookworm-slim").run_commands("echo hello")
    spec = image.to_spec()
    assert "RUN set -eux" in spec.dockerfile_content
    assert "echo hello" in spec.dockerfile_content


def test_image_to_spec_with_project_plugin():
    image = Image.from_base("debian:bookworm-slim").with_plugin(
        Project.from_git(url="https://github.com/realpython/python-scripts.git", ref="HEAD")
    )
    spec = image.to_spec()
    assert spec.request is not None
    assert "IDEGYM_PROJECT_ARCHIVE_URL" in spec.dockerfile_content
    assert "download" in spec.dockerfile_content
    assert "idegym.repository.url" in spec.labels


def test_image_to_spec_dockerfile_structure():
    image = (
        Image.from_base("debian:bookworm-slim")
        .with_plugin(BaseSystem())
        .with_plugin(User(username="appuser", uid=1000, gid=1000))
        .with_plugin(Project.from_git(url="https://github.com/realpython/python-scripts.git", ref="HEAD"))
        .run_commands("echo done")
    )
    spec = image.to_spec()
    dockerfile = spec.dockerfile_content

    # Verify key sections appear in order
    from_pos = dockerfile.index("FROM debian:bookworm-slim")
    arg_pos = dockerfile.index("ARG IDEGYM_PROJECT_ARCHIVE_URL")
    user_pos = dockerfile.rindex("USER appuser")
    run_pos = dockerfile.rindex("RUN set -eux")

    assert from_pos < arg_pos < user_pos < run_pos


def test_image_to_spec_image_version_is_stable():
    image = Image.from_base("debian:bookworm-slim").run_commands("echo hello")
    assert image.to_spec().image_version() == image.to_spec().image_version()


def test_image_to_spec_image_version_differs_on_change():
    image1 = Image.from_base("debian:bookworm-slim").run_commands("echo hello")
    image2 = Image.from_base("debian:bookworm-slim").run_commands("echo world")
    assert image1.to_spec().image_version() != image2.to_spec().image_version()


# ---------------------------------------------------------------------------
# YAML serialization round-trip
# ---------------------------------------------------------------------------


def test_image_yaml_round_trip_basic():
    image = Image.from_base("debian:bookworm-slim").named("test-image").run_commands("echo hello")
    yaml_str = image.to_yaml()
    restored = Image.from_yaml(yaml_str)
    assert restored.base == image.base
    assert restored.name == image.name
    assert restored.commands == image.commands


def test_image_yaml_round_trip_with_plugins():
    image = (
        Image.from_base("debian:bookworm-slim")
        .with_plugin(BaseSystem())
        .with_plugin(User(username="appuser", uid=1000, gid=1000))
    )
    yaml_str = image.to_yaml()
    restored = Image.from_yaml(yaml_str)
    assert len(restored.plugins) == 2
    assert isinstance(restored.plugins[0], BaseSystem)
    assert isinstance(restored.plugins[1], User)


def test_image_load_all_multi_image():
    yaml_content = dedent("""\
        images:
          - base: debian:bookworm-slim
            commands:
              - echo image1
          - base: ubuntu:jammy
            commands:
              - echo image2
    """)
    images = Image.load_all(yaml_content)
    assert len(images) == 2
    assert images[0].base == "debian:bookworm-slim"
    assert images[1].base == "ubuntu:jammy"


def test_image_from_yaml_multiple_raises():
    yaml_content = dedent("""\
        images:
          - base: debian:bookworm-slim
          - base: ubuntu:jammy
    """)
    with raises(ValueError, match="Expected exactly one image"):
        Image.from_yaml(yaml_content)


def test_image_load_all_with_project_plugin():
    yaml_content = dedent("""\
        images:
          - base: debian:bookworm-slim
            plugins:
              - type: project
                source: git
                url: https://github.com/realpython/python-scripts.git
                ref: HEAD
            commands:
              - echo done
    """)
    images = Image.load_all(yaml_content)
    assert len(images) == 1
    image = images[0]
    assert len(image.plugins) == 1
    assert isinstance(image.plugins[0], Project)
    assert image.plugins[0].source == "git"


def test_builtin_plugins_auto_registered_on_builder_import():
    # Verify that importing only `idegym.image.builder` (without importing
    # `idegym.image.plugins` explicitly) is sufficient for YAML deserialization
    # of built-in plugin types to succeed.
    script = dedent("""\
        # Intentionally do NOT import idegym.image.plugins
        from idegym.image.builder import Image

        yaml_text = '''
        images:
          - base: debian:bookworm-slim
            plugins:
              - type: base-system
                packages: [curl]
              - type: user
                username: appuser
                uid: 1000
                gid: 1000
        '''
        images = Image.load_all(yaml_text)
        assert len(images) == 1
        assert len(images[0].plugins) == 2
        print("OK")
    """)
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"Script failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
    assert result.stdout.strip() == "OK"


# ---------------------------------------------------------------------------
# Plugin serialization
# ---------------------------------------------------------------------------


@mark.parametrize(
    "plugin",
    [
        param(BaseSystem(), id="base-system-defaults"),
        param(BaseSystem(packages=("curl", "git")), id="base-system-custom"),
        param(BaseSystem(minimal=True), id="base-system-minimal"),
        param(User(username="devuser", uid=2000, gid=2000, sudo=False), id="user"),
        param(Permissions(paths={"/app": {"owner": "appuser", "mode": "755"}}), id="permissions"),
        param(
            Project.from_git(url="https://github.com/realpython/python-scripts.git", ref="HEAD"),
            id="project-git",
        ),
        param(
            Project.from_resource(
                url="https://github.com/octocat/hello-world.git",
                ref="HEAD",
                path="README.md",
            ),
            id="project-resource",
        ),
        param(Project.from_local("./src", target="/app"), id="project-local"),
        param(PyCharm(), id="pycharm-defaults"),
        param(PyCharm(version="2024.1", edition="community"), id="pycharm-custom"),
    ],
)
def test_plugin_serialize_deserialize_round_trip(plugin: PluginBase):
    payload = serialize_plugin(plugin)
    assert "type" in payload
    restored = deserialize_plugin(payload)
    assert type(restored) is type(plugin)
    assert restored == plugin


# ---------------------------------------------------------------------------
# BaseSystem.minimal
# ---------------------------------------------------------------------------


def test_base_system_minimal_uses_minimal_packages():
    plugin = BaseSystem(minimal=True)
    fragment = plugin.render(BuildContext(base="alpine:latest"))
    assert "ca-certificates" in fragment
    assert "curl" in fragment
    assert "bash" not in fragment
    assert "fontconfig" not in fragment
    assert "git" not in fragment


def test_base_system_minimal_ignores_packages_field():
    # When minimal=True, user-supplied packages are ignored
    plugin = BaseSystem(packages=("wget", "jq"), minimal=True)
    fragment = plugin.render(BuildContext(base="alpine:latest"))
    assert "wget" not in fragment
    assert "jq" not in fragment
    assert "ca-certificates" in fragment


def test_base_system_non_minimal_uses_packages():
    plugin = BaseSystem(minimal=False)
    fragment = plugin.render(BuildContext(base="debian:bookworm-slim"))
    assert "bash" in fragment
    assert "git" in fragment


# ---------------------------------------------------------------------------
# Project.from_local
# ---------------------------------------------------------------------------


def test_project_from_local():
    plugin = Project.from_local("./src", target="/app")
    assert plugin.source == "local"
    assert plugin.path == "./src"
    assert plugin.target == "/app"
    assert plugin.url is None


def test_project_from_local_apply_sets_project_root():
    plugin = Project.from_local("./src", target="/app")
    ctx = BuildContext(base="alpine:latest")
    updated = plugin.apply(ctx)
    assert updated.project_root == "/app"
    assert updated.request is None  # No download needed for local


def test_project_from_local_apply_defaults_target_to_home_work():
    plugin = Project.from_local("./src")
    ctx = BuildContext(base="alpine:latest")
    updated = plugin.apply(ctx)
    assert updated.project_root == "/root/work"


def test_project_from_local_apply_uses_user_home():
    user_plugin = User(username="appuser", uid=1000, gid=1000)
    ctx = BuildContext(base="debian:bookworm-slim")
    ctx = user_plugin.apply(ctx)
    project = Project.from_local("./src")
    updated = project.apply(ctx)
    assert updated.project_root == "/home/appuser/work"


def test_project_from_local_render_copy_directive():
    plugin = Project.from_local("./src", target="/app")
    ctx = plugin.apply(BuildContext(base="alpine:latest"))
    fragment = plugin.render(ctx)
    assert "COPY" in fragment
    assert "./src" in fragment
    assert "/app" in fragment
    assert "download" not in fragment


def test_project_from_local_render_uses_json_array_form():
    plugin = Project.from_local("./src", target="/app")
    ctx = plugin.apply(BuildContext(base="alpine:latest"))
    fragment = plugin.render(ctx)
    # JSON-array form: COPY ["src", "dest"] — brackets and quotes must be present
    assert 'COPY ["./src", "/app"]' in fragment


def test_project_from_local_render_handles_spaces_in_src_path():
    plugin = Project.from_local("my project/src", target="/app")
    ctx = plugin.apply(BuildContext(base="alpine:latest"))
    fragment = plugin.render(ctx)
    # JSON encoding preserves the space and produces a valid Dockerfile instruction
    assert 'COPY ["my project/src", "/app"]' in fragment


def test_project_from_local_render_handles_spaces_in_target():
    plugin = Project.from_local("./src", target="/my app")
    ctx = plugin.apply(BuildContext(base="alpine:latest"))
    fragment = plugin.render(ctx)
    assert 'COPY ["./src", "/my app"]' in fragment


def test_project_from_local_render_with_owner():
    plugin = Project.from_local("./src", target="/app", owner="appuser")
    ctx = plugin.apply(BuildContext(base="alpine:latest"))
    fragment = plugin.render(ctx)
    assert "--chown=appuser:appuser" in fragment
    assert 'COPY --chown=appuser:appuser ["./src", "/app"]' in fragment


def test_project_from_local_render_without_explicit_owner_no_chown():
    plugin = Project.from_local("./src", target="/app")
    ctx = plugin.apply(BuildContext(base="alpine:latest"))
    fragment = plugin.render(ctx)
    assert "--chown" not in fragment


def test_project_from_local_yaml_round_trip():
    yaml_content = dedent("""\
        images:
          - base: alpine:latest
            plugins:
              - type: project
                source: local
                path: ./src
                target: /app
    """)
    images = Image.load_all(yaml_content)
    assert len(images) == 1
    plugin = images[0].plugins[0]
    assert isinstance(plugin, Project)
    assert plugin.source == "local"
    assert plugin.path == "./src"


# ---------------------------------------------------------------------------
# Image.pip_install
# ---------------------------------------------------------------------------


def test_image_pip_install_single():
    image = Image.from_base("python:3.12-slim").pip_install("flask")
    assert "pip install flask" in image.commands


def test_image_pip_install_multiple():
    image = Image.from_base("python:3.12-slim").pip_install("flask", "requests", "pytest")
    assert "pip install flask requests pytest" in image.commands


def test_image_pip_install_in_dockerfile():
    spec = Image.from_base("python:3.12-slim").pip_install("flask").to_spec()
    assert "pip install flask" in spec.dockerfile_content


def test_image_pip_install_chains_with_run_commands():
    image = (
        Image.from_base("python:3.12-slim").run_commands("echo before").pip_install("flask").run_commands("echo after")
    )
    assert image.commands == ("echo before", "pip install flask", "echo after")


# ---------------------------------------------------------------------------
# PyCharm plugin
# ---------------------------------------------------------------------------


def test_pycharm_default_version():
    plugin = PyCharm()
    assert plugin.version == "2025.3"
    assert plugin.edition == "community"


def test_pycharm_render_contains_install_steps():
    plugin = PyCharm(version="2024.1")
    ctx = BuildContext(base="debian:bookworm-slim")
    fragment = plugin.render(ctx)
    assert 'PYCHARM_VERSION="2024.1"' in fragment
    # Archive name is now built at runtime with an arch suffix variable so that
    # amd64 and arm64 images can be built from the same Dockerfile.
    assert 'archive="pycharm-community-2024.1${suffix}.tar.gz"' in fragment
    assert "dpkg --print-architecture" in fragment
    assert "aarch64" in fragment
    assert "JAVA_HOME" in fragment
    assert "PYCHARM_DIR" in fragment
    # Must not use the curl-pipe-bash pattern (supply chain risk)
    assert "get.sdkman.io" not in fragment
    assert "| bash" not in fragment
    # Must verify the tarball checksum before extracting
    assert "sha256sum" in fragment
    assert ".sha256" in fragment
    # Java must come from PyCharm's bundled JBR, not an external install
    assert "/jbr" in fragment


def test_pycharm_render_switches_back_to_current_user():
    plugin = PyCharm()
    user_plugin = User(username="appuser", uid=1000, gid=1000)
    ctx = BuildContext(base="debian:bookworm-slim")
    ctx = user_plugin.apply(ctx)
    fragment = plugin.render(ctx)
    assert fragment.strip().endswith("USER appuser")


def test_pycharm_render_uses_explicit_user_field():
    plugin = PyCharm(user="developer")
    ctx = BuildContext(base="debian:bookworm-slim")
    fragment = plugin.render(ctx)
    assert fragment.strip().endswith("USER developer")


def test_pycharm_render_root_only_container():
    plugin = PyCharm()
    ctx = BuildContext(base="debian:bookworm-slim")  # default current_user="root"
    fragment = plugin.render(ctx)
    assert fragment.strip().endswith("USER root")


# ---------------------------------------------------------------------------
# Input validation (shell injection prevention)
# ---------------------------------------------------------------------------


@mark.parametrize(
    "username",
    [
        param("root; rm -rf /", id="semicolon-injection"),
        param("user name", id="space"),
        param("user$(id)", id="command-substitution"),
        param("user`id`", id="backtick"),
        param("User", id="uppercase"),
        param("1user", id="starts-with-digit"),
        param("", id="empty"),
        param("x" * 33, id="too-long"),
        param("user\nroot", id="newline"),
        param("user&&id", id="and-operator"),
    ],
)
def test_user_rejects_invalid_username(username):
    with raises(ValueError):
        User(username=username, uid=1000, gid=1000)


@mark.parametrize(
    "username",
    [
        param("appuser", id="simple"),
        param("test-user", id="hyphen"),
        param("test_user", id="underscore"),
        param("_user", id="starts-with-underscore"),
        param("user1", id="with-digit"),
        param("a" * 32, id="max-length"),
    ],
)
def test_user_accepts_valid_username(username):
    user = User(username=username, uid=1000, gid=1000)
    assert user.username == username


def test_user_rejects_invalid_group():
    with raises(ValueError, match="group"):
        User(username="appuser", uid=1000, gid=1000, group="bad group!")


def test_user_rejects_invalid_additional_group():
    with raises(ValueError, match="additional_groups"):
        User(username="appuser", uid=1000, gid=1000, additional_groups=("valid", "bad; id"))


def test_permissions_rejects_shell_injection_in_owner():
    with raises(ValueError, match="owner"):
        Permissions(paths={"/home/user": {"owner": "user; rm -rf /"}})


def test_permissions_rejects_shell_injection_in_group():
    with raises(ValueError, match="group"):
        Permissions(paths={"/home/user": {"group": "group$(id)"}})


@mark.parametrize(
    "mode",
    [
        param("999", id="non-octal-digit"),
        param("abc", id="non-numeric"),
        param("75", id="too-short"),
        param("77777", id="too-long"),
        param("07 5", id="space"),
    ],
)
def test_permissions_rejects_invalid_mode(mode):
    with raises(ValueError, match="mode"):
        Permissions(paths={"/home/user": {"mode": mode}})


@mark.parametrize(
    "mode",
    [
        param("755", id="three-digits"),
        param("0755", id="four-digits"),
        param("644", id="read-write"),
        param("0440", id="sudoers"),
    ],
)
def test_permissions_accepts_valid_mode(mode):
    perm = Permissions(paths={"/home/user": {"mode": mode}})
    assert perm.paths["/home/user"]["mode"] == mode


def test_project_rejects_invalid_owner():
    with raises(ValueError, match="owner"):
        Project(source="git", url="https://example.com/repo.git", owner="user; id")


def test_project_rejects_invalid_group():
    with raises(ValueError, match="owner/group"):
        Project(source="git", url="https://example.com/repo.git", group="bad group")


def test_project_rejects_path_starting_with_double_dash():
    with raises(ValueError, match="--"):
        Project.from_local("--from=0")


def test_project_rejects_target_starting_with_double_dash():
    with raises(ValueError, match="--"):
        Project.from_local("./src", target="--from=0")


@mark.parametrize(
    "package",
    [
        param("; rm -rf /", id="semicolon-injection"),
        param("Curl", id="uppercase"),
        param("curl git", id="space"),
        param("curl\ngit", id="newline"),
        param("a", id="too-short"),
    ],
)
def test_basesystem_rejects_invalid_package(package):
    with raises(ValueError, match="package"):
        BaseSystem(packages=(package,))


@mark.parametrize(
    "version",
    [
        param("2025.3; rm -rf /", id="injection"),
        param("latest", id="non-numeric"),
        param("25.3", id="short-year"),
        param("2025", id="no-minor"),
        param("2025.3.1.2", id="too-many-parts"),
    ],
)
def test_pycharm_rejects_invalid_version(version):
    with raises(ValueError, match="version"):
        PyCharm(version=version)


@mark.parametrize("version", [param("2025.3", id="two-parts"), param("2025.3.1", id="three-parts")])
def test_pycharm_accepts_valid_version(version):
    assert PyCharm(version=version).version == version


def test_pycharm_rejects_invalid_edition():
    with raises(ValueError, match="edition"):
        PyCharm(edition="enterprise")


def test_pycharm_rejects_injection_in_user():
    with raises(ValueError, match="user"):
        PyCharm(user="root; id")


def test_pycharm_render_installs_open_project_plugin_when_project_in_ctx():
    plugin = PyCharm(open_project=True)
    ctx = BuildContext(base="debian:bookworm-slim")
    ctx = ctx.with_extra("idegym.has_project", True)
    fragment = plugin.render(ctx)
    assert "open-project.zip" in fragment
    assert "trusted-paths.xml" in fragment
    # Plugin installed to bundled dir so the "open" AppStarter is discovered on launch
    assert "${PYCHARM_DIR}/plugins/" in fragment
    # Start script and supervisord config are COPY'd from the build context
    assert "COPY plugins/pycharm/scripts/start-pycharm.sh" in fragment
    assert "COPY plugins/pycharm/scripts/supervisord-pycharm.conf" in fragment


def test_pycharm_render_skips_open_project_plugin_when_open_project_false():
    plugin = PyCharm(open_project=False)
    ctx = BuildContext(base="debian:bookworm-slim")
    ctx = ctx.with_extra("idegym.has_project", True)
    fragment = plugin.render(ctx)
    assert "open-project.zip" not in fragment


def test_pycharm_render_skips_open_project_plugin_when_no_project():
    plugin = PyCharm(open_project=True)
    ctx = BuildContext(base="debian:bookworm-slim")
    # No project in context
    fragment = plugin.render(ctx)
    assert "open-project.zip" not in fragment


# ---------------------------------------------------------------------------
# IDEA plugin
# ---------------------------------------------------------------------------


def test_idea_default_version():
    plugin = Idea()
    assert plugin.version == "2025.3"


def test_idea_render_contains_install_steps():
    plugin = Idea(version="2025.2.4")
    ctx = BuildContext(base="debian:bookworm-slim")
    fragment = plugin.render(ctx)
    assert 'IDEA_VERSION="2025.2.4"' in fragment
    # Archive name built at runtime with arch suffix variable.
    assert 'archive="ideaIC-2025.2.4${suffix}.tar.gz"' in fragment
    assert "dpkg --print-architecture" in fragment
    assert "aarch64" in fragment
    assert "JAVA_HOME" in fragment
    assert "IDE_DIR" in fragment
    # Must verify the tarball checksum before extracting
    assert "sha256sum" in fragment
    assert ".sha256" in fragment
    # Java must come from IDEA's bundled JBR
    assert "/jbr" in fragment
    # IDEA supports headless mode (unlike PyCharm CE)
    assert "java.awt.headless=true" in fragment


def test_idea_render_switches_back_to_current_user():
    plugin = Idea()
    user_plugin = User(username="appuser", uid=1000, gid=1000)
    ctx = BuildContext(base="debian:bookworm-slim")
    ctx = user_plugin.apply(ctx)
    fragment = plugin.render(ctx)
    assert fragment.strip().endswith("USER appuser")


def test_idea_render_uses_explicit_user_field():
    plugin = Idea(user="developer")
    ctx = BuildContext(base="debian:bookworm-slim")
    fragment = plugin.render(ctx)
    assert fragment.strip().endswith("USER developer")


def test_idea_render_root_only_container():
    plugin = Idea()
    ctx = BuildContext(base="debian:bookworm-slim")
    fragment = plugin.render(ctx)
    assert fragment.strip().endswith("USER root")


def test_idea_render_includes_mcp_plugin_when_update_id_set():
    plugin = Idea(mcp_update_id="882474")
    ctx = BuildContext(base="debian:bookworm-slim")
    fragment = plugin.render(ctx)
    assert "882474" in fragment
    assert "mcpServer.xml" in fragment
    assert "enableMcpServer" in fragment


def test_idea_render_skips_mcp_plugin_when_update_id_none():
    plugin = Idea(mcp_update_id=None)
    ctx = BuildContext(base="debian:bookworm-slim")
    fragment = plugin.render(ctx)
    assert "mcpServer.xml" not in fragment
    assert "enableMcpServer" not in fragment


def test_idea_render_installs_open_project_plugin_when_project_in_ctx():
    plugin = Idea(open_project=True)
    ctx = BuildContext(base="debian:bookworm-slim")
    ctx = ctx.with_extra("idegym.has_project", True)
    fragment = plugin.render(ctx)
    assert "open-project.zip" in fragment
    assert "trusted-paths.xml" in fragment
    # Plugin is installed to bundled dir so it's loaded before the "open" command dispatches
    assert "${IDE_DIR}/plugins/" in fragment
    # Start script and supervisord config are COPY'd from the build context
    assert "COPY plugins/idea/scripts/start-idea.sh" in fragment
    assert "COPY plugins/idea/scripts/supervisord-idea.conf" in fragment


def test_idea_render_skips_open_project_plugin_when_open_project_false():
    plugin = Idea(open_project=False)
    ctx = BuildContext(base="debian:bookworm-slim")
    ctx = ctx.with_extra("idegym.has_project", True)
    fragment = plugin.render(ctx)
    assert "open-project.zip" not in fragment


def test_idea_render_skips_open_project_plugin_when_no_project():
    plugin = Idea(open_project=True)
    ctx = BuildContext(base="debian:bookworm-slim")
    # No project in context
    fragment = plugin.render(ctx)
    assert "open-project.zip" not in fragment


@mark.parametrize(
    "version",
    [
        param("2025.3; rm -rf /", id="injection"),
        param("latest", id="non-numeric"),
        param("25.3", id="short-year"),
        param("2025", id="no-minor"),
        param("2025.3.1.2", id="too-many-parts"),
    ],
)
def test_idea_rejects_invalid_version(version):
    with raises(ValueError, match="version"):
        Idea(version=version)


@mark.parametrize("version", [param("2025.3", id="two-parts"), param("2025.3.1", id="three-parts")])
def test_idea_accepts_valid_version(version):
    assert Idea(version=version).version == version


def test_idea_rejects_injection_in_user():
    with raises(ValueError, match="user"):
        Idea(user="root; id")


# ---------------------------------------------------------------------------
# Project.from_archive
# ---------------------------------------------------------------------------


def test_project_from_archive():
    plugin = Project.from_archive("https://example.com/project.tar.gz")
    assert plugin.source == "archive"
    assert plugin.url == "https://example.com/project.tar.gz"
    assert plugin.target is None
    assert plugin.owner is None


def test_project_from_archive_apply_sets_project_root():
    plugin = Project.from_archive("https://example.com/project.tar.gz", target="/opt/project")
    ctx = plugin.apply(BuildContext(base="debian:bookworm-slim"))
    assert ctx.project_root == "/opt/project"


def test_project_from_archive_apply_defaults_target_to_home_work():
    plugin = Project.from_archive("https://example.com/project.tar.gz")
    ctx = plugin.apply(BuildContext(base="debian:bookworm-slim"))
    assert ctx.project_root == "/root/work"


def test_project_from_archive_apply_uses_user_home():
    user_plugin = User(username="devuser", uid=1000, gid=1000)
    ctx = user_plugin.apply(BuildContext(base="debian:bookworm-slim"))
    plugin = Project.from_archive("https://example.com/project.tar.gz")
    ctx = plugin.apply(ctx)
    assert ctx.project_root == "/home/devuser/work"


def test_project_from_archive_apply_no_download_request():
    plugin = Project.from_archive("https://example.com/project.tar.gz")
    ctx = plugin.apply(BuildContext(base="debian:bookworm-slim"))
    assert ctx.request is None


def test_project_from_archive_render_uses_curl_and_extract():
    plugin = Project.from_archive("https://example.com/project.tar.gz")
    ctx = plugin.apply(BuildContext(base="debian:bookworm-slim"))
    fragment = plugin.render(ctx)
    assert "curl" in fragment
    assert "extract" in fragment
    assert "https://example.com/project.tar.gz" in fragment


def test_project_from_archive_render_cleans_up_tmp_file():
    plugin = Project.from_archive("https://example.com/project.tar.gz")
    ctx = plugin.apply(BuildContext(base="debian:bookworm-slim"))
    fragment = plugin.render(ctx)
    assert "rm" in fragment
    assert "/tmp/project-archive" in fragment


def test_project_from_archive_render_with_owner():
    plugin = Project.from_archive("https://example.com/project.tar.gz", owner="devuser")
    ctx = plugin.apply(BuildContext(base="debian:bookworm-slim"))
    fragment = plugin.render(ctx)
    assert "chown" in fragment
    assert "devuser" in fragment


def test_project_from_archive_render_without_owner_uses_current_user():
    user_plugin = User(username="devuser", uid=1000, gid=1000)
    ctx = user_plugin.apply(BuildContext(base="debian:bookworm-slim"))
    plugin = Project.from_archive("https://example.com/project.tar.gz")
    fragment = plugin.render(plugin.apply(ctx))
    assert "chown" in fragment
    assert "devuser" in fragment


def test_project_from_archive_yaml_round_trip():
    yaml_content = dedent("""\
        images:
          - base: debian:bookworm-slim
            plugins:
              - type: project
                source: archive
                url: https://example.com/project.tar.gz
                target: /opt/project
    """)
    images = Image.load_all(yaml_content)
    plugin = images[0].plugins[0]
    assert isinstance(plugin, Project)
    assert plugin.source == "archive"
    assert plugin.url == "https://example.com/project.tar.gz"
    assert plugin.target == "/opt/project"


def test_project_from_archive_serialize_round_trip():
    plugin = Project.from_archive("https://example.com/project.tar.gz", target="/opt/project", owner="devuser")
    payload = serialize_plugin(plugin)
    restored = deserialize_plugin(payload)
    assert type(restored) is Project
    assert restored == plugin


# ---------------------------------------------------------------------------
# Project.from_git_clone
# ---------------------------------------------------------------------------


def test_project_from_git_clone():
    plugin = Project.from_git_clone(url="https://github.com/owner/repo.git", ref="main")
    assert plugin.source == "git-clone"
    assert plugin.url == "https://github.com/owner/repo.git"
    assert plugin.ref == "main"


def test_project_from_git_clone_default_ref():
    plugin = Project.from_git_clone(url="https://github.com/owner/repo.git")
    assert plugin.ref == "HEAD"


def test_project_from_git_clone_apply_sets_project_root():
    plugin = Project.from_git_clone(url="https://github.com/owner/repo.git", target="/opt/repo")
    ctx = plugin.apply(BuildContext(base="debian:bookworm-slim"))
    assert ctx.project_root == "/opt/repo"


def test_project_from_git_clone_apply_no_download_request():
    plugin = Project.from_git_clone(url="https://github.com/owner/repo.git")
    ctx = plugin.apply(BuildContext(base="debian:bookworm-slim"))
    assert ctx.request is None


def test_project_from_git_clone_render_contains_git_clone():
    plugin = Project.from_git_clone(url="https://github.com/owner/repo.git", target="/opt/repo")
    ctx = plugin.apply(BuildContext(base="debian:bookworm-slim"))
    fragment = plugin.render(ctx)
    assert "git clone" in fragment
    assert "https://github.com/owner/repo.git" in fragment
    assert "/opt/repo" in fragment


def test_project_from_git_clone_render_non_head_ref_adds_checkout():
    plugin = Project.from_git_clone(url="https://github.com/owner/repo.git", ref="abc123", target="/opt/repo")
    ctx = plugin.apply(BuildContext(base="debian:bookworm-slim"))
    fragment = plugin.render(ctx)
    assert "checkout" in fragment
    assert "abc123" in fragment


def test_project_from_git_clone_render_head_ref_no_checkout():
    plugin = Project.from_git_clone(url="https://github.com/owner/repo.git", ref="HEAD", target="/opt/repo")
    ctx = plugin.apply(BuildContext(base="debian:bookworm-slim"))
    fragment = plugin.render(ctx)
    assert "checkout" not in fragment


def test_project_from_git_clone_render_with_owner():
    plugin = Project.from_git_clone(url="https://github.com/owner/repo.git", target="/opt/repo", owner="devuser")
    ctx = plugin.apply(BuildContext(base="debian:bookworm-slim"))
    fragment = plugin.render(ctx)
    assert "chown" in fragment
    assert "devuser" in fragment


def test_project_from_git_clone_render_without_owner_uses_current_user():
    user_plugin = User(username="devuser", uid=1000, gid=1000)
    ctx = user_plugin.apply(BuildContext(base="debian:bookworm-slim"))
    plugin = Project.from_git_clone(url="https://github.com/owner/repo.git")
    fragment = plugin.render(plugin.apply(ctx))
    assert "chown" in fragment
    assert "devuser" in fragment


def test_project_from_git_clone_serialize_round_trip():
    plugin = Project.from_git_clone(url="https://github.com/owner/repo.git", ref="main", target="/opt/repo")
    payload = serialize_plugin(plugin)
    restored = deserialize_plugin(payload)
    assert type(restored) is Project
    assert restored == plugin


# ---------------------------------------------------------------------------
# IdeGYMServer.from_git (Dockerfile generation only — no real git clone)
# ---------------------------------------------------------------------------


def test_idegym_server_from_git():
    plugin = IdeGYMServer.from_git(url="https://github.com/owner/idegym.git", ref="main")
    assert plugin.source == "git"
    assert plugin.url == "https://github.com/owner/idegym.git"
    assert plugin.ref == "main"


def test_idegym_server_from_git_default_ref():
    plugin = IdeGYMServer.from_git(url="https://github.com/owner/idegym.git")
    assert plugin.ref == "HEAD"


def test_idegym_server_from_git_apply_returns_ctx_unchanged():
    plugin = IdeGYMServer.from_git(url="https://github.com/owner/idegym.git")
    ctx = BuildContext(base="debian:bookworm-slim")
    result = plugin.apply(ctx)
    assert result is ctx


def test_idegym_server_from_git_render_contains_git_clone():
    plugin = IdeGYMServer.from_git(url="https://github.com/owner/idegym.git")
    ctx = BuildContext(base="debian:bookworm-slim")
    fragment = plugin.render(ctx)
    assert "git clone" in fragment
    assert "https://github.com/owner/idegym.git" in fragment


def test_idegym_server_from_git_render_non_head_ref_adds_checkout():
    plugin = IdeGYMServer.from_git(url="https://github.com/owner/idegym.git", ref="v1.2.3")
    ctx = BuildContext(base="debian:bookworm-slim")
    fragment = plugin.render(ctx)
    assert "checkout" in fragment
    assert "v1.2.3" in fragment


def test_idegym_server_from_git_render_head_ref_no_checkout():
    plugin = IdeGYMServer.from_git(url="https://github.com/owner/idegym.git", ref="HEAD")
    ctx = BuildContext(base="debian:bookworm-slim")
    fragment = plugin.render(ctx)
    assert "checkout" not in fragment


def test_idegym_server_from_git_render_contains_idegym_env():
    plugin = IdeGYMServer.from_git(url="https://github.com/owner/idegym.git")
    ctx = BuildContext(base="debian:bookworm-slim")
    fragment = plugin.render(ctx)
    assert "IDEGYM_PATH" in fragment
    assert "PYTHONPATH" in fragment
    assert "uv" in fragment


def test_idegym_server_from_git_render_contains_uv_sync():
    plugin = IdeGYMServer.from_git(url="https://github.com/owner/idegym.git")
    ctx = BuildContext(base="debian:bookworm-slim")
    fragment = plugin.render(ctx)
    assert "uv sync" in fragment
    assert "supervisor" in fragment


def test_idegym_server_from_git_render_contains_entrypoint():
    plugin = IdeGYMServer.from_git(url="https://github.com/owner/idegym.git")
    ctx = BuildContext(base="debian:bookworm-slim")
    fragment = plugin.render(ctx)
    assert "ENTRYPOINT" in fragment
    assert "dumb-init" in fragment
    assert "HEALTHCHECK" in fragment
    assert "EXPOSE 8000" in fragment


def test_idegym_server_from_git_render_uses_current_user():
    user_plugin = User(username="devuser", uid=1000, gid=1000)
    ctx = user_plugin.apply(BuildContext(base="debian:bookworm-slim"))
    plugin = IdeGYMServer.from_git(url="https://github.com/owner/idegym.git")
    fragment = plugin.render(ctx)
    assert "devuser" in fragment
    assert "USER devuser" in fragment


def test_idegym_server_from_git_render_requires_url():
    plugin = IdeGYMServer(source="git", url=None)
    ctx = BuildContext(base="debian:bookworm-slim")
    with raises(ValueError, match="requires a URL"):
        plugin.render(ctx)


def test_idegym_server_from_git_serialize_round_trip():
    plugin = IdeGYMServer.from_git(url="https://github.com/owner/idegym.git", ref="main")
    payload = serialize_plugin(plugin)
    restored = deserialize_plugin(payload)
    assert type(restored) is IdeGYMServer
    assert restored == plugin


# ---------------------------------------------------------------------------
# MCPUpstream plugin
# ---------------------------------------------------------------------------


def test_mcp_upstream_render_writes_config_file():
    plugin = MCPUpstream(name="my-tool", url="http://localhost:9000/mcp")
    ctx = BuildContext(base="debian:bookworm-slim")
    fragment = plugin.render(ctx)
    assert "mkdir -p /etc/idegym/mcp-upstreams.d" in fragment
    assert "/etc/idegym/mcp-upstreams.d/my-tool.json" in fragment
    assert "http://localhost:9000/mcp" in fragment


def test_mcp_upstream_render_valid_json_in_fragment():
    import json

    plugin = MCPUpstream(name="svc", url="http://localhost:1234/mcp")
    ctx = BuildContext(base="debian:bookworm-slim")
    fragment = plugin.render(ctx)
    # The JSON blob should be present and well-formed
    assert '"url"' in fragment
    assert json.dumps({"url": "http://localhost:1234/mcp"}) in fragment


def test_mcp_upstream_invalid_name_raises():
    with raises(Exception):
        MCPUpstream(name="My_Tool", url="http://localhost/mcp")  # uppercase not allowed


def test_mcp_upstream_invalid_name_with_slash_raises():
    with raises(Exception):
        MCPUpstream(name="path/traversal", url="http://localhost/mcp")


def test_mcp_upstream_serialize_round_trip():
    plugin = MCPUpstream(name="test-svc", url="http://localhost:8080/mcp")
    payload = serialize_plugin(plugin)
    assert payload["type"] == "mcp-upstream"
    restored = deserialize_plugin(payload)
    assert type(restored) is MCPUpstream
    assert restored == plugin


# ---------------------------------------------------------------------------
# PluginBase.get_mcp_upstream default
# ---------------------------------------------------------------------------


def test_plugin_base_get_mcp_upstream_default_is_none():
    assert PluginBase.get_mcp_upstream() is None


# ---------------------------------------------------------------------------
# PyCharm MCP upstream / server plugin router
# ---------------------------------------------------------------------------


def test_pycharm_get_mcp_upstream():
    assert PyCharm.get_mcp_upstream() == "http://localhost:64342"


def test_pycharm_server_plugin_get_server_router_returns_router():
    from idegym.plugins.pycharm.server import PyCharmPlugin

    # FastAPI is available in the test environment (server is installed)
    router = PyCharmPlugin.get_server_router()
    assert router is not None
    route_paths = [r.path for r in router.routes]
    assert "/pycharm/health" in route_paths


# ---------------------------------------------------------------------------
# IDEA MCP upstream / server plugin router
# ---------------------------------------------------------------------------


def test_idea_get_mcp_upstream():
    assert Idea.get_mcp_upstream() == "http://localhost:64342"


def test_idea_server_plugin_get_server_router_returns_router():
    from idegym.plugins.idea.server import IdeaPlugin

    router = IdeaPlugin.get_server_router()
    assert router is not None
    route_paths = [r.path for r in router.routes]
    assert "/idea/health" in route_paths


def test_idea_to_spec_auto_emits_mcp_config():
    """When Idea plugin declares get_mcp_upstream(), to_spec() writes the config file."""
    image = Image.from_base("debian:bookworm-slim").with_plugin(Idea())
    spec = image.to_spec()
    assert "/etc/idegym/mcp-upstreams.d/idea.json" in spec.dockerfile_content
    assert "http://localhost:64342" in spec.dockerfile_content


# ---------------------------------------------------------------------------
# Image.to_spec() auto-emits MCP upstream config
# ---------------------------------------------------------------------------


def test_to_spec_auto_emits_mcp_config_for_plugin_with_mcp_upstream():
    """When a plugin declares get_mcp_upstream(), to_spec() writes the config file."""
    image = Image.from_base("debian:bookworm-slim").with_plugin(PyCharm())
    spec = image.to_spec()
    assert "/etc/idegym/mcp-upstreams.d/pycharm.json" in spec.dockerfile_content
    assert "http://localhost:64342" in spec.dockerfile_content


def test_to_spec_mcp_config_not_emitted_for_plugin_without_upstream():
    """Plugins that return None from get_mcp_upstream() do not produce a config fragment."""
    image = Image.from_base("debian:bookworm-slim").with_plugin(BaseSystem())
    spec = image.to_spec()
    assert "/etc/idegym/mcp-upstreams.d/" not in spec.dockerfile_content


def test_to_spec_mcp_config_uses_registered_type_name():
    """The config file is named after the plugin's registered type name."""
    image = Image.from_base("debian:bookworm-slim").with_plugin(PyCharm())
    spec = image.to_spec()
    # "pycharm" is the registered @image_plugin name
    assert "mcp-upstreams.d/pycharm.json" in spec.dockerfile_content


def test_to_spec_explicit_mcp_upstream_plugin():
    """MCPUpstream plugin generates its config fragment in the Dockerfile."""
    plugin = MCPUpstream(name="test-svc", url="http://localhost:8080/mcp")
    image = Image.from_base("debian:bookworm-slim").with_plugin(plugin)
    spec = image.to_spec()
    assert "mcp-upstreams.d/test-svc.json" in spec.dockerfile_content
    assert "http://localhost:8080/mcp" in spec.dockerfile_content


def test_mcp_fragment_no_user_switch_when_root():
    """When current_user is root, _mcp_upstream_fragment emits no USER directives."""
    from idegym.image.builder import _mcp_upstream_fragment

    ctx = BuildContext(base="debian:bookworm-slim", current_user="root")
    fragment = _mcp_upstream_fragment(PyCharm(), ctx)
    assert "mcp-upstreams.d/pycharm.json" in fragment
    assert "USER" not in fragment


def test_mcp_fragment_wraps_with_user_switch_for_non_root():
    """When current_user is not root, _mcp_upstream_fragment wraps with USER root / USER <user>."""
    from idegym.image.builder import _mcp_upstream_fragment

    ctx = BuildContext(base="debian:bookworm-slim", current_user="appuser")
    fragment = _mcp_upstream_fragment(PyCharm(), ctx)
    assert "mcp-upstreams.d/pycharm.json" in fragment
    assert "USER root" in fragment
    assert "USER appuser" in fragment
    # USER root must precede the write; USER appuser must follow it
    write_idx = fragment.index("mcp-upstreams.d/pycharm.json")
    assert fragment.index("USER root") < write_idx
    assert fragment.rindex("USER appuser") > write_idx


# ---------------------------------------------------------------------------
# @image_plugin name validation (fail-fast at registration time)
# ---------------------------------------------------------------------------


def test_image_plugin_rejects_name_with_slash():
    with raises(ValueError, match="invalid"):

        @image_plugin("path/traversal")
        class _BadPlugin(PluginBase):
            pass


def test_image_plugin_rejects_name_with_dotdot():
    with raises(ValueError, match="invalid"):

        @image_plugin("../escape")
        class _BadPlugin2(PluginBase):
            pass


def test_image_plugin_rejects_name_starting_with_digit():
    with raises(ValueError, match="invalid"):

        @image_plugin("1starts-with-digit")
        class _BadPlugin3(PluginBase):
            pass


def test_image_plugin_rejects_uppercase_name():
    with raises(ValueError, match="invalid"):

        @image_plugin("MyPlugin")
        class _BadPlugin4(PluginBase):
            pass


def test_image_plugin_accepts_valid_name():
    @image_plugin("test-valid-name-99")
    class _GoodPlugin(PluginBase):
        pass

    # Verify it was registered without error (clean up to avoid polluting other tests)
    from idegym.api.plugin import _PLUGIN_REGISTRY, _PLUGIN_TYPE_NAMES

    _PLUGIN_REGISTRY.pop("test-valid-name-99", None)
    _PLUGIN_TYPE_NAMES.pop(_GoodPlugin, None)


# ---------------------------------------------------------------------------
# _mcp_upstream_fragment path-traversal guard
# ---------------------------------------------------------------------------


def test_to_spec_raises_for_unregistered_plugin_with_unsafe_class_name():
    """An unregistered plugin whose class name is not a safe filename raises ValueError at to_spec()."""

    class UnsafeNamePlugin_With_Underscores(PluginBase):
        @classmethod
        def get_mcp_upstream(cls) -> str:
            return "http://localhost:1234/mcp"

    image = Image.from_base("debian:bookworm-slim").with_plugin(UnsafeNamePlugin_With_Underscores())
    with raises(ValueError, match="safe filename"):
        image.to_spec()


# ---------------------------------------------------------------------------
# Server plugin registry
# ---------------------------------------------------------------------------


def test_server_plugin_decorator_registers_class():
    @server_plugin
    class _TestPlugin:
        @classmethod
        def get_server_router(cls):
            return "dummy-router"

    all_plugins = get_all_server_plugins()
    assert _TestPlugin in all_plugins


def test_get_all_server_plugins_nonempty():
    import idegym.plugins.defaults.server  # noqa: F401 — registers ToolsPlugin, RewardsPlugin

    assert len(get_all_server_plugins()) > 0


# ---------------------------------------------------------------------------
# PycharmClientOperations
# ---------------------------------------------------------------------------


def test_pycharm_client_operations_health_calls_forward():
    """PycharmClientOperations.health() calls forward_request with the expected arguments."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from idegym.plugins.pycharm.client import PycharmClientOperations

    mock_forward = MagicMock()
    mock_forward.forward_request = AsyncMock(return_value={"mcp_url": "http://localhost:6789/mcp"})

    ops = PycharmClientOperations(
        forward=mock_forward,
        server_id=42,
        client_id=None,
        polling_config=None,
    )

    result = asyncio.run(ops.health())

    assert result == {"mcp_url": "http://localhost:6789/mcp"}
    mock_forward.forward_request.assert_called_once_with(
        method="GET",
        server_id=42,
        path="pycharm/health",
        client_id=None,
        polling_config=None,
    )


def test_pycharm_client_operations_class_is_instantiable_with_mocks():
    """PycharmClientOperations accepts duck-typed constructor args without importing client."""
    from unittest.mock import MagicMock

    from idegym.plugins.pycharm.client import PycharmClientOperations

    ops = PycharmClientOperations(
        forward=MagicMock(),
        server_id=1,
        client_id=MagicMock(),
        polling_config=MagicMock(),
    )
    assert ops._server_id == 1


# ---------------------------------------------------------------------------
# @server_plugin / get_all_server_plugins scope isolation
# ---------------------------------------------------------------------------


def test_server_plugin_class_not_in_image_registry():
    """A class decorated with @server_plugin does not appear in the image plugin registry."""
    from idegym.api.plugin import get_all_registered_plugin_classes

    @server_plugin
    class _ScopeIsolationServerPlugin:
        @classmethod
        def get_server_router(cls):
            return None

    assert _ScopeIsolationServerPlugin not in get_all_registered_plugin_classes()


def test_image_plugin_not_in_server_registry():
    """PyCharm (image plugin, not a @server_plugin) does not appear in get_all_server_plugins()."""
    all_server = get_all_server_plugins()
    assert PyCharm not in all_server


def test_get_all_server_plugins_does_not_include_builtin_image_plugins():
    """Built-in image plugins (BaseSystem, User, etc.) are not in the server plugin list."""
    all_server = get_all_server_plugins()
    image_only_classes = [BaseSystem, User, Permissions, MCPUpstream, Project]
    for cls in image_only_classes:
        assert cls not in all_server, f"{cls.__name__} should not be in server plugin registry"


# ---------------------------------------------------------------------------
# PyCharm.apply() — extras accumulation for plugins.json
# ---------------------------------------------------------------------------


def test_pycharm_apply_adds_enabled_server_plugins_extra():
    """apply() sets 'pycharm' in idegym.enabled_server_plugins list."""
    ctx = BuildContext(base="debian:bookworm-slim")
    result = PyCharm().apply(ctx)
    assert result.get_extra("idegym.enabled_server_plugins") == ["pycharm"]


def test_pycharm_apply_is_idempotent():
    """Calling apply() twice does not duplicate 'pycharm' in the extras list."""
    ctx = BuildContext(base="debian:bookworm-slim")
    ctx1 = PyCharm().apply(ctx)
    ctx2 = PyCharm().apply(ctx1)
    assert ctx2.get_extra("idegym.enabled_server_plugins") == ["pycharm"]


def test_pycharm_apply_does_not_modify_original_context():
    """apply() returns a new context; the original is unchanged (immutability)."""
    ctx = BuildContext(base="debian:bookworm-slim")
    PyCharm().apply(ctx)
    assert ctx.get_extra("idegym.enabled_server_plugins") is None


def test_pycharm_apply_preserves_other_extras():
    """apply() does not remove unrelated extras from the context."""
    ctx = BuildContext(base="debian:bookworm-slim").with_extra("custom.key", "value")
    result = PyCharm().apply(ctx)
    assert result.get_extra("custom.key") == "value"


def test_non_pycharm_plugins_do_not_set_server_plugins_extra():
    """Image plugins other than PyCharm do not touch idegym.enabled_server_plugins."""
    ctx = BuildContext(base="debian:bookworm-slim")
    plugins = [
        BaseSystem(),
        User(username="appuser"),
        Permissions(paths={"/tmp": {"owner": "appuser"}}),
        MCPUpstream(name="svc", url="http://x"),
    ]
    for plugin in plugins:
        result = plugin.apply(ctx)
        assert result.get_extra("idegym.enabled_server_plugins") is None, (
            f"{type(plugin).__name__}.apply() should not set idegym.enabled_server_plugins"
        )


# ---------------------------------------------------------------------------
# IdeGYMServer._render_plugins_config()
# ---------------------------------------------------------------------------


def _git_idegym_server() -> IdeGYMServer:
    """Return a minimal IdeGYMServer configured for git source (avoids local path checks)."""
    return IdeGYMServer.from_git(url="https://example.com/idegym.git", ref="HEAD")


def test_idegym_server_render_plugins_config_defaults():
    """Without any extras, plugins config contains only tools and rewards."""
    ctx = BuildContext(base="debian:bookworm-slim")
    fragment = _git_idegym_server()._render_plugins_config(ctx)
    assert '"server": ["tools", "rewards"]' in fragment


def test_idegym_server_render_plugins_config_includes_pycharm_when_in_extras():
    """With pycharm in idegym.enabled_server_plugins extras, config includes pycharm after base plugins."""
    ctx = BuildContext(base="debian:bookworm-slim").with_extra("idegym.enabled_server_plugins", ["pycharm"])
    fragment = _git_idegym_server()._render_plugins_config(ctx)
    assert '"server": ["tools", "rewards", "pycharm"]' in fragment


def test_idegym_server_render_plugins_config_no_duplicates_for_base_plugins():
    """tools and rewards are not duplicated even if they appear in extras."""
    ctx = BuildContext(base="debian:bookworm-slim").with_extra(
        "idegym.enabled_server_plugins", ["tools", "pycharm", "rewards"]
    )
    fragment = _git_idegym_server()._render_plugins_config(ctx)
    assert '"server": ["tools", "rewards", "pycharm"]' in fragment
    assert fragment.count('"tools"') == 1
    assert fragment.count('"rewards"') == 1


def test_idegym_server_render_plugins_config_base_plugins_always_first():
    """tools and rewards always appear before extra plugins in the JSON config."""
    ctx = BuildContext(base="debian:bookworm-slim").with_extra("idegym.enabled_server_plugins", ["pycharm"])
    fragment = _git_idegym_server()._render_plugins_config(ctx)
    tools_pos = fragment.index('"tools"')
    rewards_pos = fragment.index('"rewards"')
    pycharm_pos = fragment.index('"pycharm"')
    assert tools_pos < rewards_pos < pycharm_pos


def test_idegym_server_render_plugins_config_produces_valid_json():
    """The generated fragment embeds syntactically valid JSON."""
    import json
    import re

    ctx = BuildContext(base="debian:bookworm-slim").with_extra("idegym.enabled_server_plugins", ["pycharm"])
    fragment = _git_idegym_server()._render_plugins_config(ctx)
    # Extract the JSON string from the printf '...' command
    match = re.search(r"printf '%s\\n' '({.*?})'", fragment)
    assert match is not None, f"Could not find JSON in plugins config fragment:\n{fragment}"
    parsed = json.loads(match.group(1))
    assert parsed == {"server": ["tools", "rewards", "pycharm"]}


def test_idegym_server_render_plugins_config_chowns_to_current_user():
    """plugins.json and its parent dir are chowned to ctx.current_user so appuser can overwrite it later."""
    ctx = BuildContext(base="debian:bookworm-slim", current_user="appuser")
    fragment = _git_idegym_server()._render_plugins_config(ctx)
    assert "chown appuser:appuser /etc/idegym /etc/idegym/plugins.json" in fragment


def test_idegym_server_render_plugins_config_chowns_with_separate_group():
    """When a custom group is set via extras, chown uses user:group."""
    ctx = BuildContext(base="debian:bookworm-slim", current_user="appuser").with_extra("idegym.user.group", "staff")
    fragment = _git_idegym_server()._render_plugins_config(ctx)
    assert "chown appuser:staff /etc/idegym /etc/idegym/plugins.json" in fragment


# ---------------------------------------------------------------------------
# plugins.json written inside full Image.to_spec() pipeline
# ---------------------------------------------------------------------------


def test_to_spec_idegym_server_from_git_writes_plugins_json():
    """to_spec() with IdeGYMServer from git writes /etc/idegym/plugins.json with tools+rewards."""
    image = Image.from_base("debian:bookworm-slim").with_plugin(
        IdeGYMServer.from_git(url="https://example.com/idegym.git", ref="HEAD")
    )
    spec = image.to_spec()
    assert "/etc/idegym/plugins.json" in spec.dockerfile_content
    assert '"tools"' in spec.dockerfile_content
    assert '"rewards"' in spec.dockerfile_content


def test_to_spec_pycharm_before_idegym_server_writes_pycharm_to_plugins_json():
    """When PyCharm appears before IdeGYMServer in the pipeline, pycharm is added to plugins.json."""
    image = (
        Image.from_base("debian:bookworm-slim")
        .with_plugin(PyCharm())
        .with_plugin(IdeGYMServer.from_git(url="https://example.com/idegym.git", ref="HEAD"))
    )
    spec = image.to_spec()
    assert '"pycharm"' in spec.dockerfile_content
    # Verify the full expected list appears
    assert '"server": ["tools", "rewards", "pycharm"]' in spec.dockerfile_content


def test_to_spec_no_pycharm_plugin_omits_pycharm_from_plugins_json():
    """Without a PyCharm plugin in the pipeline, pycharm does not appear in plugins.json."""
    image = (
        Image.from_base("debian:bookworm-slim")
        .with_plugin(User(username="appuser"))
        .with_plugin(IdeGYMServer.from_git(url="https://example.com/idegym.git", ref="HEAD"))
    )
    spec = image.to_spec()
    # Only base plugins in the JSON
    assert '"server": ["tools", "rewards"]' in spec.dockerfile_content
    # pycharm must NOT appear in the JSON itself (may appear in MCP config elsewhere)
    assert '"pycharm"' not in spec.dockerfile_content


# ---------------------------------------------------------------------------
# Entry_points — all expected image plugins registered after builder import
# ---------------------------------------------------------------------------


def test_all_expected_image_plugins_are_registered():
    """All default and optional image plugins are registered in the @image_plugin registry."""
    from idegym.api.plugin import get_plugin_class

    expected_names = ["base-system", "user", "permissions", "mcp-upstream", "project", "idegym-server", "pycharm"]
    for name in expected_names:
        assert get_plugin_class(name) is not None, f"Image plugin '{name}' not found in registry"


def test_image_plugin_registry_maps_pycharm_name_to_pycharm_class():
    """The 'pycharm' key in the image plugin registry resolves to the PyCharm class."""
    from idegym.api.plugin import get_plugin_class

    assert get_plugin_class("pycharm") is PyCharm


def test_image_plugin_registry_maps_default_names_to_correct_classes():
    """Each default image plugin name maps to its expected class."""
    from idegym.api.plugin import get_plugin_class

    expected = {
        "base-system": BaseSystem,
        "user": User,
        "permissions": Permissions,
        "mcp-upstream": MCPUpstream,
        "project": Project,
        "idegym-server": IdeGYMServer,
    }
    for name, cls in expected.items():
        assert get_plugin_class(name) is cls, f"Expected '{name}' → {cls.__name__}"
