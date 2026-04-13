import subprocess
import sys
from textwrap import dedent

from idegym.image.builder import Image
from idegym.image.plugin import BuildContext, PluginBase
from idegym.image.plugins import BaseSystem, IdeGYMServer, Permissions, Project, PyCharm, User
from idegym.image.serialization import deserialize_plugin, serialize_plugin
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
    assert plugin.edition == "professional"


def test_pycharm_render_contains_install_steps():
    plugin = PyCharm(version="2024.1")
    ctx = BuildContext(base="debian:bookworm-slim")
    fragment = plugin.render(ctx)
    assert 'PYCHARM_VERSION="2024.1"' in fragment
    assert "pycharm-professional" in fragment
    assert "sdkman" in fragment.lower()
    assert "JAVA_HOME" in fragment
    assert "PYCHARM_DIR" in fragment


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
