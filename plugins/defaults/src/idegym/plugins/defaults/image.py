import json
import re
from pathlib import Path
from shlex import quote
from textwrap import dedent
from typing import ClassVar, Optional
from urllib.parse import urlsplit, urlunsplit

from idegym.api.download import Authorization, DownloadRequest
from idegym.api.git import GitRepository, GitRepositoryResource, GitRepositorySnapshot
from idegym.api.plugin import MCP_UPSTREAMS_DIR, BuildContext, PluginBase, image_plugin, split_user
from idegym.api.type import AuthType
from idegym.plugins.plugin_utils import check_linux_id
from idegym.utils.dockerfile import declared_instructions, logical_lines, parser_directives
from pydantic import Field, field_validator

# Valid Debian package name: starts with alphanumeric, rest are lowercase alphanumeric, +, -, .
_DEBIAN_PACKAGE_RE = re.compile(r"^[a-z0-9][a-z0-9+.-]+$")
# chmod mode: 3 or 4 octal digits
_OCTAL_MODE_RE = re.compile(r"^[0-7]{3,4}$")


def _build_image_labels(value: GitRepository | GitRepositorySnapshot | GitRepositoryResource) -> dict[str, str]:
    match value:
        case repository if isinstance(value, GitRepository):
            return {"idegym.repository.url": repository.url}
        case snapshot if isinstance(value, GitRepositorySnapshot):
            labels = _build_image_labels(snapshot.repository)
            return {**labels, "idegym.repository.revision": snapshot.reference}
        case resource if isinstance(value, GitRepositoryResource):
            labels = _build_image_labels(resource.snapshot)
            return {**labels, "idegym.repository.resource": resource.path}
        case _:
            raise ValueError(f"Unsupported project type: {type(value).__name__}")


def _render_run_block(commands: list[str], *, comment: Optional[str] = None) -> str:
    filtered = [command.strip() for command in commands if command.strip()]
    if not filtered:
        return ""
    body = " && \\\n    ".join(filtered)
    prefix = f"# {comment}\n" if comment else ""
    return f"{prefix}RUN set -eux; \\\n    {body}"


@image_plugin("base-system")
class BaseSystem(PluginBase):
    """Install base system packages via ``apt-get``.

    Emits an ``apt-get install`` block followed by ``update-ca-certificates``.

    Attributes:
        packages: Tuple of Debian package names to install. Defaults to ``DEFAULT_PACKAGES``.
        minimal: When ``True``, installs only ``ca-certificates`` and ``curl`` regardless of
            the ``packages`` field.
    """

    DEFAULT_PACKAGES: ClassVar[tuple[str, ...]] = (
        "bash",
        "ca-certificates",
        "coreutils",
        "curl",
        "dumb-init",
        "findutils",
        "git",
        "netcat-openbsd",
        "sudo",
    )
    MINIMAL_PACKAGES: ClassVar[tuple[str, ...]] = (
        "ca-certificates",
        "curl",
    )

    packages: tuple[str, ...] = DEFAULT_PACKAGES
    minimal: bool = False

    @field_validator("packages")
    @classmethod
    def _validate_packages(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        for pkg in v:
            if not _DEBIAN_PACKAGE_RE.match(pkg):
                raise ValueError(
                    f"Invalid Debian package name: {pkg!r}. "
                    r"Must match ^[a-z0-9][a-z0-9+.-]+$"
                )
        return v

    def render(self, ctx: BuildContext) -> str:
        return ctx.as_root(self._fragment(ctx))

    def _fragment(self, ctx: BuildContext) -> str:
        packages = self.MINIMAL_PACKAGES if self.minimal else self.packages
        if not packages:
            return ""
        package_list = " \\\n".join(f"                    {package}" for package in packages)
        return dedent(
            f"""\
            # Install base system packages
            RUN set -eux; \\
                apt-get update -qq; \\
                apt-get install -y --no-install-recommends \\
{package_list}; \\
                apt-get clean; \\
                rm -rf /var/lib/apt/lists/*

            # Refresh system caches
            RUN set -eux; \\
                update-ca-certificates
            """
        ).strip()


@image_plugin("user")
class User(PluginBase):
    """Create or update a Linux user and group inside the image.

    Uses idempotent shell commands: if the user/group already exists it is updated in-place,
    so images can be layered on top of each other without conflicts.

    Updates ``BuildContext.current_user``, ``BuildContext.current_group`` and ``BuildContext.home``
    after ``apply()``, and moves ``BuildContext.project_root`` to ``<home>/work`` unless a project
    plugin before it has already placed the project.

    Attributes:
        username: Linux username (must match ``^[a-z_][a-z0-9_-]{0,31}$``).
        uid: Numeric user ID. Defaults to ``1000``.
        gid: Numeric group ID. Defaults to ``1000``.
        group: Primary group name. Defaults to ``username``.
        home: Home directory. Defaults to ``/home/<username>``.
        shell: Login shell. Defaults to ``/bin/bash``.
        sudo: Add a passwordless sudoers entry when ``True`` (default).
        create_home: Create the home directory and set ownership when ``True`` (default).
        additional_groups: Extra groups to add the user to.
    """

    username: str
    uid: int = 1000
    gid: int = 1000
    group: Optional[str] = None
    home: Optional[str] = None
    shell: str = "/bin/bash"
    sudo: bool = True
    create_home: bool = True
    additional_groups: tuple[str, ...] = ()

    @field_validator("username")
    @classmethod
    def _validate_username(cls, v: str) -> str:
        return check_linux_id(v, "username")

    @field_validator("group")
    @classmethod
    def _validate_group(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            check_linux_id(v, "group")
        return v

    @field_validator("additional_groups")
    @classmethod
    def _validate_additional_groups(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        for g in v:
            check_linux_id(g, "additional_groups")
        return v

    @property
    def effective_group(self) -> str:
        return self.group or self.username

    @property
    def effective_home(self) -> str:
        return self.home or f"/home/{self.username}"

    def apply(self, ctx: BuildContext) -> BuildContext:
        project_root = ctx.project_root if ctx.get_extra("idegym.has_project") else f"{self.effective_home}/work"
        return ctx.updated(
            current_user=self.username,
            current_group=self.group,
            home=self.effective_home,
            project_root=project_root,
        ).with_extras(
            {
                "idegym.user.uid": self.uid,
                "idegym.user.gid": self.gid,
            }
        )

    def render(self, ctx: BuildContext) -> str:
        return ctx.as_root(self._fragment(ctx))

    def _fragment(self, ctx: BuildContext) -> str:
        group = self.effective_group
        home = self.effective_home
        additional_groups = ",".join(self.additional_groups)
        create_home_flag = "-m" if self.create_home else "-M"
        commands = [
            (
                f"if getent group {group} >/dev/null 2>&1; then "
                f'current_gid="$(getent group {group} | cut -d: -f3)"; '
                f'if [ "$current_gid" != "{self.gid}" ]; then groupmod -g {self.gid} {group}; fi; '
                f"else groupadd -g {self.gid} {group}; fi"
            ),
        ]

        for extra_group in self.additional_groups:
            commands.append(f"if ! getent group {extra_group} >/dev/null 2>&1; then groupadd {extra_group}; fi")

        group_flags = f"-G {additional_groups}" if additional_groups else ""
        add_groups = f"usermod -aG {additional_groups} {self.username}" if additional_groups else ":"

        commands.append(
            f"if id -u {self.username} >/dev/null 2>&1; then "
            f"usermod -u {self.uid} -g {group} -d {quote(home)} -s {quote(self.shell)} {self.username}; "
            f"{add_groups}; "
            "else "
            f"useradd -u {self.uid} -g {group} {group_flags} -d {quote(home)} "
            f"-s {quote(self.shell)} {create_home_flag} {self.username}; "
            "fi"
        )
        if self.create_home:
            commands.extend(
                [
                    f"mkdir -p {quote(home)}",
                    f"chown -R {self.username}:{group} {quote(home)}",
                ]
            )

        if self.sudo:
            commands.append(f'echo "{self.username} ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/{self.username}')
            commands.append(f"chmod 0440 /etc/sudoers.d/{self.username}")
        else:
            commands.append(f"rm -f /etc/sudoers.d/{self.username}")

        return _render_run_block(commands, comment=f"Create or update user {self.username}")


@image_plugin("permissions")
class Permissions(PluginBase):
    """Set file ownership and permissions inside the image.

    Emits ``chown`` and ``chmod`` commands for the specified paths.

    Attributes:
        paths: Mapping from path to a config dict with optional keys ``owner``, ``group``,
            and ``mode`` (3- or 4-digit octal string, e.g. ``"755"``). If ``group`` is
            omitted but ``owner`` is set, the group defaults to the owner value.
    """

    paths: dict[str, dict[str, Optional[str]]]

    @field_validator("paths")
    @classmethod
    def _validate_paths(cls, v: dict[str, dict[str, Optional[str]]]) -> dict[str, dict[str, Optional[str]]]:
        for config in v.values():
            for key in ("owner", "group"):
                val = config.get(key)
                if val is not None:
                    check_linux_id(val, key)
            mode = config.get("mode")
            if mode is not None and not _OCTAL_MODE_RE.match(mode):
                raise ValueError(f"Invalid file mode: {mode!r}. Expected 3 or 4 octal digits (e.g. '755').")
        return v

    def render(self, ctx: BuildContext) -> str:
        return ctx.as_root(self._fragment(ctx))

    def _fragment(self, ctx: BuildContext) -> str:
        commands: list[str] = []
        for path, config in self.paths.items():
            owner = config.get("owner")
            group = config.get("group") or owner
            mode = config.get("mode")

            if owner is not None:
                if group is not None:
                    commands.append(f"chown -R {owner}:{group} {quote(path)}")
                else:
                    commands.append(f"chown -R {owner} {quote(path)}")
            elif group is not None:
                commands.append(f"chgrp -R {group} {quote(path)}")

            if mode is not None:
                commands.append(f"chmod -R {mode} {quote(path)}")

        return _render_run_block(commands, comment="Adjust file ownership and permissions")


@image_plugin("raw-lines")
class RawLines(PluginBase):
    """Insert Dockerfile instructions into the image, verbatim, where the plugin sits in the list.

    For what no other plugin expresses — an ``ENV``, an ``ARG``, a one-off ``RUN`` — without
    writing a plugin for it. The lines run as root, like every plugin's fragment: the generated
    stage opens with ``USER root``, and one is re-emitted before any fragment that would otherwise
    start as someone else. A ``USER`` in the lines becomes ``BuildContext.current_user`` (and its
    group, if given, ``BuildContext.current_group``), so it is the user later plugins switch back to
    and the one the image ends as.

    Attributes:
        lines: Dockerfile lines, emitted in order. Continuations and heredocs are fine; ``FROM``
            is refused, since it would start a new stage and cut everything after it off from the
            image, and so are parser directives, which Docker only reads at the top of the file.
    """

    lines: tuple[str, ...]

    @field_validator("lines")
    @classmethod
    def _validate_lines(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        text = "\n".join(v)
        if parser_directives(text):
            raise ValueError(
                "Parser directives (# syntax=, # escape=) only take effect at the top of a Dockerfile; "
                "put them in the base Dockerfile instead."
            )
        for line in logical_lines(text):
            if line.text.partition(" ")[0].upper() == "FROM":
                raise ValueError(
                    f"'raw-lines' cannot contain FROM (line {line.number}): it would start a new "
                    "stage, leaving every later plugin out of the image."
                )
        return v

    def apply(self, ctx: BuildContext) -> BuildContext:
        line = declared_instructions("\n".join(self.lines), ["USER"]).get("USER")
        user = line.text.partition(" ")[2].strip() if line is not None else ""
        if not user:
            return ctx
        name, group = split_user(user)
        return ctx.updated(current_user=name, current_group=group)

    def render(self, ctx: BuildContext) -> str:
        return ctx.as_root(self._fragment(ctx))

    def _fragment(self, ctx: BuildContext) -> str:
        return "\n".join(self.lines).strip()


# Regex for MCP upstream service names: lowercase letters + digits + hyphens, starts with a letter.
_MCP_SERVICE_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")


@image_plugin("mcp-upstream")
class MCPUpstream(PluginBase):
    """Write an MCP upstream configuration file into the image.

    Creates ``/etc/idegym/mcp-upstreams.d/{name}.json`` containing the given URL.
    Use this plugin to declare that a service running inside the container exposes
    an MCP server, so the IdeGYM server (and external tooling) can discover it.

    Image-build plugins that ship their own MCP server (e.g. ``pycharm``) can
    instead override ``get_mcp_upstream()`` — the image builder will emit this
    same config file automatically.

    Attributes:
        name: Service identifier used as the config filename (e.g. ``"my-tool"`` →
            ``/etc/idegym/mcp-upstreams.d/my-tool.json``).
            Must be lowercase letters, digits, and hyphens; starts with a letter.
        url: MCP server URL accessible inside the running container
            (e.g. ``"http://localhost:8080/mcp"``).
    """

    name: str
    url: str

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        if not _MCP_SERVICE_NAME_RE.match(v):
            raise ValueError(
                f"Invalid MCP upstream name: {v!r}. "
                r"Must match ^[a-z][a-z0-9-]{0,62}$ (lowercase letters, digits, hyphens; starts with a letter)."
            )
        return v

    def render(self, ctx: BuildContext) -> str:
        return ctx.as_root(self._fragment(ctx))

    def _fragment(self, ctx: BuildContext) -> str:
        config = json.dumps({"url": self.url})
        return _render_run_block(
            [
                f"mkdir -p {MCP_UPSTREAMS_DIR}",
                f"printf '%s\\n' {quote(config)} > {MCP_UPSTREAMS_DIR}/{self.name}.json",
            ],
            comment=f"Register MCP upstream: {self.name}",
        )


@image_plugin("project")
class Project(PluginBase):
    """Fetch and place a project inside the image.

    Supports several ``source`` modes:

    - ``"git"`` / ``"resource"`` — download via the IdeGYM download service at runtime;
      sets ``BuildContext.request`` and emits download+extract commands using build-arg env vars.
      Only one ``Project`` plugin with these modes is allowed per image.
    - ``"local"`` — emit a ``COPY`` directive from the Docker build context. No download request.
    - ``"archive"`` — ``curl`` a static archive URL and extract it inside the container.
    - ``"git-clone"`` — ``git clone`` directly inside the container at build time.

    Use the class methods (``from_git``, ``from_resource``, ``from_local``, ``from_archive``,
    ``from_git_clone``) rather than constructing directly.

    Updates ``BuildContext.project_root`` to ``target`` (or ``<home>/work`` if unset).
    """

    source: str
    url: Optional[str] = None
    ref: str = "HEAD"
    path: Optional[str] = None
    auth: Authorization = Field(default_factory=Authorization)
    target: Optional[str] = None
    owner: Optional[str] = None
    group: Optional[str] = None

    @field_validator("owner", "group")
    @classmethod
    def _validate_owner_group(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            check_linux_id(v, "owner/group")
        return v

    @field_validator("path", "target")
    @classmethod
    def _validate_no_double_dash_prefix(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v.startswith("--"):
            raise ValueError(f"Path must not start with '--': {v!r}")
        return v

    @classmethod
    def from_git(
        cls,
        *,
        url: str,
        ref: str = "HEAD",
        auth: Optional[Authorization] = None,
        auth_type: Optional[AuthType] = None,
        auth_token: Optional[str] = None,
        target: Optional[str] = None,
        owner: Optional[str] = None,
        group: Optional[str] = None,
    ) -> "Project":
        return cls(
            source="git",
            url=url,
            ref=ref,
            auth=auth or Authorization(type=auth_type, token=auth_token),
            target=target,
            owner=owner,
            group=group,
        )

    @classmethod
    def from_resource(
        cls,
        *,
        url: str,
        path: str,
        ref: str = "HEAD",
        auth: Optional[Authorization] = None,
        auth_type: Optional[AuthType] = None,
        auth_token: Optional[str] = None,
        target: Optional[str] = None,
        owner: Optional[str] = None,
        group: Optional[str] = None,
    ) -> "Project":
        if auth is not None and (auth_type is not None or auth_token is not None):
            raise ValueError("Use either 'auth' or 'auth_type'/'auth_token', not both")
        return cls(
            source="resource",
            url=url,
            ref=ref,
            path=path,
            auth=auth or Authorization(type=auth_type, token=auth_token),
            target=target,
            owner=owner,
            group=group,
        )

    @classmethod
    def from_local(
        cls,
        path: str,
        *,
        target: Optional[str] = None,
        owner: Optional[str] = None,
        group: Optional[str] = None,
    ) -> "Project":
        return cls(source="local", path=path, target=target, owner=owner, group=group)

    @classmethod
    def from_archive(
        cls,
        url: str,
        *,
        target: Optional[str] = None,
        owner: Optional[str] = None,
        group: Optional[str] = None,
    ) -> "Project":
        return cls(source="archive", url=url, target=target, owner=owner, group=group)

    @classmethod
    def from_git_clone(
        cls,
        *,
        url: str,
        ref: str = "HEAD",
        target: Optional[str] = None,
        owner: Optional[str] = None,
        group: Optional[str] = None,
    ) -> "Project":
        return cls(source="git-clone", url=url, ref=ref, target=target, owner=owner, group=group)

    def project(self) -> GitRepositorySnapshot | GitRepositoryResource:
        if self.url is None:
            raise ValueError(f"Project source '{self.source}' requires a URL")
        repository = GitRepository.parse(self.url)
        snapshot = repository.at(self.ref)
        if self.source == "git":
            return snapshot
        if self.source == "resource":
            if self.path is None:
                raise ValueError("Resource project requires 'path'")
            return snapshot.resource(self.path)
        raise ValueError(f"Unsupported project source: {self.source}")

    def apply(self, ctx: BuildContext) -> BuildContext:
        if self.source in ("local", "archive", "git-clone"):
            project_root = self.target or f"{ctx.home}/work"
            return ctx.updated(project_root=project_root).with_extra("idegym.has_project", True)

        if ctx.request is not None:
            raise ValueError("Only one Project plugin is supported")

        project = self.project()
        request = DownloadRequest(
            descriptor=project.descriptor(),
            auth=self.auth,
        )
        project_root = self.target or f"{ctx.home}/work"
        return ctx.updated(
            request=request,
            labels={**ctx.labels, **_build_image_labels(project)},
            project_root=project_root,
        ).with_extra("idegym.has_project", True)

    def render(self, ctx: BuildContext) -> str:
        return ctx.as_root(self._fragment(ctx))

    def _fragment(self, ctx: BuildContext) -> str:
        if self.source == "local":
            src_path = self.path or "."
            # JSON-array form handles paths with spaces; flags must precede the array.
            copy_args = json.dumps([src_path, ctx.project_root])
            chown = ""
            if self.owner:
                effective_group = self.group or self.owner
                chown = f"--chown={self.owner}:{effective_group} "
            return f"# Copy local project\nCOPY {chown}{copy_args}"

        if self.source == "archive":
            if self.url is None:
                raise ValueError("archive source requires a URL")
            commands = [
                f"mkdir -p {quote(ctx.project_root)}",
                f"curl -fsSL {quote(self.url)} -o /tmp/project-archive",
                f"extract /tmp/project-archive {quote(ctx.project_root)}",
                "rm -f /tmp/project-archive",
            ]
            commands.append(f"chown -R {self._owner(ctx)} {quote(ctx.project_root)}")
            return _render_run_block(commands, comment="Download and extract project archive")

        if self.source == "git-clone":
            if self.url is None:
                raise ValueError("git-clone source requires a URL")
            commands = [
                f"git clone {quote(self.url)} {quote(ctx.project_root)}",
            ]
            if self.ref and self.ref != "HEAD":
                commands.append(f"git -C {quote(ctx.project_root)} checkout {quote(self.ref)}")
            commands.append(f"chown -R {self._owner(ctx)} {quote(ctx.project_root)}")
            return _render_run_block(commands, comment=f"Clone {self.url}")

        if ctx.request is None:
            raise ValueError("Project plugin must be applied before rendering")

        commands = [
            f"mkdir -p {quote(ctx.project_root)}",
            (
                "download $IDEGYM_PROJECT_ARCHIVE_URL $IDEGYM_PROJECT_ARCHIVE_PATH "
                "--auth-type ${IDEGYM_AUTH_TYPE:-} --auth-token ${IDEGYM_AUTH_TOKEN:-}"
            ),
            "extract $IDEGYM_PROJECT_ARCHIVE_PATH $IDEGYM_PROJECT_ROOT",
        ]
        commands.append(f"chown -R {self._owner(ctx)} {quote(ctx.project_root)}")

        return _render_run_block(commands, comment="Fetch and unpack the project")

    def _owner(self, ctx: BuildContext) -> str:
        """The ``user:group`` the project is chowned to: ``owner``/``group``, else the current user's."""
        if self.owner is None:
            return f"{ctx.current_user}:{self.group or ctx.current_group or ctx.current_user}"
        return f"{self.owner}:{self.group or self.owner}"


# Every path the renderer copies out of an IdeGYM checkout. A ref that predates any of them —
# an example config pinning a commit from before `plugins/` was split out, say — used to fail
# deep inside the Docker build with a bare `cp: no such file`, naming neither the ref nor what
# it was missing. Keep this in step with the copies in `_render_from_git` / `_render_from_local`.
_REQUIRED_WORKSPACE_PATHS = (
    ".python-version",
    "api",
    "backend-utils",
    "common-utils",
    "entrypoint.py",
    "entrypoint.sh",
    "idegym.sh",
    "plugins",
    "pyproject.toml",
    "rewards",
    "scripts",
    "server",
    "supervisord.conf",
    "tools",
    "uv.lock",
)


def _redact_userinfo(url: str) -> str:
    """The URL with any ``user:password@`` replaced, for text that is not the clone itself.

    The clone needs the credential, but a build log line or a Dockerfile comment does not, and both
    are kept verbatim in the image history and in build output.
    """
    parts = urlsplit(url)
    if "@" not in parts.netloc:
        return url
    return urlunsplit(parts._replace(netloc="***@" + parts.netloc.rpartition("@")[2]))


def _render_workspace_path_check(source_root: str, described_as: str) -> str:
    """Fail the build with the missing paths listed, instead of on the first `cp` that misses.

    Runs immediately after the checkout so the failure arrives in seconds, and reports *every*
    missing path at once so an out-of-date ref does not have to be diagnosed one `cp` at a time.
    ``described_as`` comes from the caller's URL and ref, so it reaches the shell only as a
    quoted ``printf`` argument: inside a double-quoted ``echo``, a ``$`` or ``"`` in it would be
    expanded or break the step.
    """
    checks = " ".join(quote(path) for path in _REQUIRED_WORKSPACE_PATHS)
    return dedent(
        f"""\
        RUN set -eu; \\
            missing=""; \\
            for path in {checks}; do \\
                [ -e {quote(source_root)}/"$path" ] || missing="$missing $path"; \\
            done; \\
            if [ -n "$missing" ]; then \\
                printf 'IdeGYM source at %s is missing:%s\\n' {quote(described_as)} "$missing" >&2; \\
                echo "This ref predates the current workspace layout; pick a newer one." >&2; \\
                exit 1; \\
            fi
        """
    ).rstrip()


def _idegym_server_env(project_root: str) -> str:
    return dedent(
        f"""\
        COPY --from=ghcr.io/astral-sh/uv:0.10.11 /uv /uvx /bin/

        ENV IDEGYM_PATH=/opt/idegym \\
            IDEGYM_PROJECT_ROOT={project_root} \\
            PYTHONDONTWRITEBYTECODE=0 \\
            PYTHONUNBUFFERED=1 \\
            PYTHONHASHSEED=random
        ENV PYTHONPATH="$IDEGYM_PATH"
        """
    ).rstrip()


def _idegym_server_uv_sync() -> str:
    return dedent(
        """\
        RUN set -eux; \\
            uv python install; \\
            uv sync --project server \\
                --frozen \\
                --no-cache \\
                --no-dev; \\
            uv pip install supervisor
        """
    ).rstrip()


def _idegym_server_tail() -> str:
    return dedent(
        """\
        VOLUME /docker-entrypoint.d
        EXPOSE 8000

        ENTRYPOINT ["dumb-init", "--"]
        CMD ["entrypoint", ".venv/bin/supervisord", "-c", "supervisord.conf"]

        HEALTHCHECK \\
            --start-period=10s \\
            --interval=60s \\
            --timeout=30s \\
            --retries=5 \\
        CMD nc -z 127.0.0.1 8000 || exit 1
        """
    ).rstrip()


@image_plugin("idegym-server")
class IdeGYMServer(PluginBase):
    """Embed the IdeGYM server into the image.

    Installs the server runtime, supervisord configuration, entrypoint scripts, and sets
    up the container's ``CMD`` / ``HEALTHCHECK``. This plugin must be the last one in the
    pipeline as it emits the full container entrypoint.

    Also writes ``/etc/idegym/plugins.json`` listing the server plugins to enable at
    runtime. Built-in plugins (``tools``, ``rewards``) are always included. Optional
    plugins that called ``apply()`` earlier (e.g. ``PyCharm``) are read from
    ``ctx.extras["idegym.enabled_server_plugins"]``.

    Use ``from_local()`` to copy from a local workspace (the build context is set to
    ``root``), or ``from_git()`` to clone from a remote repository inside the container.
    """

    source: str
    root: Optional[str] = None
    url: Optional[str] = None
    ref: Optional[str] = None

    @classmethod
    def from_local(cls, root: Optional[str | Path] = None) -> "IdeGYMServer":
        root_path = Path.cwd() if root is None else Path(root)
        return cls(source="local", root=str(root_path.expanduser().resolve()))

    @classmethod
    def from_git(cls, *, url: str, ref: str = "HEAD") -> "IdeGYMServer":
        return cls(source="git", url=url, ref=ref)

    def apply(self, ctx: BuildContext) -> BuildContext:
        if self.source == "git":
            # No host build context needed; everything is cloned inside the container.
            return ctx
        if self.root is None:
            raise ValueError("IdeGYMServer.from_local(...) requires a workspace root")
        self._validate_local_root(Path(self.root))
        return ctx.updated(context_path=self.root)

    @staticmethod
    def _validate_local_root(root: Path) -> None:
        """Reject a workspace root that the Dockerfile's COPYs would fail on, before building.

        A local root is on the host, so this can be checked outright rather than deferred to the
        build — the same check the git path has to make inside the container.
        """
        missing = [path for path in _REQUIRED_WORKSPACE_PATHS if not (root / path).exists()]
        if missing:
            raise ValueError(
                f"IdeGYM source at {root} is missing: {', '.join(missing)}. "
                "IdeGYMServer.from_local(...) needs the root of an IdeGYM workspace."
            )

    def render(self, ctx: BuildContext) -> str:
        return ctx.as_root(self._fragment(ctx))

    def _fragment(self, ctx: BuildContext) -> str:
        if self.source == "git":
            if self.url is None:
                raise ValueError("IdeGYMServer.from_git(...) requires a URL")
            return self._render_from_git(ctx)
        return self._render_from_local(ctx)

    def _render_plugins_config(self, ctx: BuildContext) -> str:
        base_plugins = ["tools", "rewards"]
        extra_plugins = list(ctx.get_extra("idegym.enabled_server_plugins", []))
        all_plugins = base_plugins + [p for p in extra_plugins if p not in base_plugins]
        config = json.dumps({"server": all_plugins})
        return _render_run_block(
            [
                "mkdir -p /etc/idegym",
                f"printf '%s\\n' {quote(config)} > /etc/idegym/plugins.json",
                f"chown {ctx.owner} /etc/idegym /etc/idegym/plugins.json",
            ],
            comment="Write enabled server plugins config",
        )

    def _render_from_git(self, ctx: BuildContext) -> str:
        owner = ctx.owner
        ref = self.ref or "HEAD"
        clone_lines = [f"git clone {quote(self.url)} /tmp/idegym-src"]
        if ref != "HEAD":
            clone_lines.append(f"git -C /tmp/idegym-src checkout {quote(ref)}")
        clone_run = _render_run_block(clone_lines, comment=f"Clone IdeGYM from {_redact_userinfo(self.url)}")
        setup = dedent(
            f"""\
            RUN set -eux; \\
                mkdir -p $IDEGYM_PATH $IDEGYM_PROJECT_ROOT; \\
                cp -r /tmp/idegym-src/scripts/. /usr/local/bin/; \\
                cp /tmp/idegym-src/entrypoint.py $IDEGYM_PATH/; \\
                cp /tmp/idegym-src/entrypoint.sh /tmp/idegym-src/idegym.sh /usr/local/bin/; \\
                chmod 755 /usr/local/bin/* $IDEGYM_PATH/entrypoint.py; \\
                chown {owner} /usr/local/bin/* $IDEGYM_PATH/entrypoint.py; \\
                for script in /usr/local/bin/*.{{py,sh}}; do \\
                    [ -f "$script" ] || continue; \\
                    mv "$script" "$(echo "${{script%.*}}" | tr "_" "-")"; \\
                done; \\
                cp /tmp/idegym-src/.python-version /tmp/idegym-src/pyproject.toml \\
                    /tmp/idegym-src/supervisord.conf /tmp/idegym-src/uv.lock $IDEGYM_PATH/; \\
                cp -r /tmp/idegym-src/api $IDEGYM_PATH/api; \\
                cp -r /tmp/idegym-src/backend-utils $IDEGYM_PATH/backend-utils; \\
                cp -r /tmp/idegym-src/common-utils $IDEGYM_PATH/common-utils; \\
                cp -r /tmp/idegym-src/plugins $IDEGYM_PATH/plugins; \\
                cp -r /tmp/idegym-src/rewards $IDEGYM_PATH/rewards; \\
                cp -r /tmp/idegym-src/tools $IDEGYM_PATH/tools; \\
                cp -r /tmp/idegym-src/server $IDEGYM_PATH/server; \\
                chown -R {owner} $IDEGYM_PATH $IDEGYM_PROJECT_ROOT; \\
                rm -rf /tmp/idegym-src
            """
        ).rstrip()
        return "\n\n".join(
            [
                _idegym_server_env(ctx.project_root),
                clone_run,
                _render_workspace_path_check("/tmp/idegym-src", f"{_redact_userinfo(self.url)}@{ref}"),
                setup,
                self._render_plugins_config(ctx),
                f"USER {ctx.user_spec}\nWORKDIR $IDEGYM_PATH",
                _idegym_server_uv_sync(),
                _idegym_server_tail(),
            ]
        )

    def _render_from_local(self, ctx: BuildContext) -> str:
        owner = ctx.owner
        local_setup = dedent(
            f"""\
            RUN set -eux; \\
                mkdir -p $IDEGYM_PATH $IDEGYM_PROJECT_ROOT; \\
                chown -R {owner} $IDEGYM_PATH $IDEGYM_PROJECT_ROOT

            COPY --chown={owner} --chmod=755 scripts /usr/local/bin/
            COPY --chown={owner} --chmod=755 entrypoint.py $IDEGYM_PATH/
            COPY --chown={owner} --chmod=755 entrypoint.sh idegym.sh /usr/local/bin/

            RUN set -eux; \\
                for script in /usr/local/bin/*.{{py,sh}}; do \\
                    [ -f "$script" ] || continue; \\
                    mv "$script" "$(echo "${{script%.*}}" | tr "_" "-")"; \\
                done
            """
        ).rstrip()
        workspace_copies = dedent(
            f"""\
            COPY --chown={owner} .python-version pyproject.toml supervisord.conf uv.lock ./
            COPY --chown={owner} api api/
            COPY --chown={owner} backend-utils backend-utils/
            COPY --chown={owner} common-utils common-utils/
            COPY --chown={owner} plugins plugins/
            COPY --chown={owner} rewards rewards/
            COPY --chown={owner} tools tools/
            COPY --chown={owner} server server/
            """
        ).rstrip()
        return "\n\n".join(
            [
                _idegym_server_env(ctx.project_root),
                local_setup,
                self._render_plugins_config(ctx),
                f"USER {ctx.user_spec}\nWORKDIR $IDEGYM_PATH",
                workspace_copies,
                _idegym_server_uv_sync(),
                _idegym_server_tail(),
            ]
        )
