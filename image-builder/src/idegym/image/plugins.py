import json
import re
from pathlib import Path
from shlex import quote
from textwrap import dedent
from typing import ClassVar, Optional, Union

from idegym.api.download import Authorization, DownloadRequest
from idegym.api.git import GitRepository, GitRepositoryResource, GitRepositorySnapshot
from idegym.api.type import AuthType
from idegym.image.plugin import BuildContext, PluginBase, image_plugin
from pydantic import Field, field_validator

# Linux username/group: starts with letter or underscore, then letters/digits/hyphens/underscores, max 32 chars.
_LINUX_IDENTIFIER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
# Valid Debian package name: starts with alphanumeric, rest are lowercase alphanumeric, +, -, .
_DEBIAN_PACKAGE_RE = re.compile(r"^[a-z0-9][a-z0-9+.-]+$")
# chmod mode: 3 or 4 octal digits
_OCTAL_MODE_RE = re.compile(r"^[0-7]{3,4}$")
# PyCharm version: YYYY.N or YYYY.N.N
_PYCHARM_VERSION_RE = re.compile(r"^\d{4}\.\d+(\.\d+)?$")


def _check_linux_id(value: str, field: str) -> str:
    if not _LINUX_IDENTIFIER_RE.match(value):
        raise ValueError(
            f"Invalid Linux identifier for {field!r}: {value!r}. "
            r"Must match ^[a-z_][a-z0-9_-]{0,31}$"
        )
    return value


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
        return _check_linux_id(v, "username")

    @field_validator("group")
    @classmethod
    def _validate_group(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            _check_linux_id(v, "group")
        return v

    @field_validator("additional_groups")
    @classmethod
    def _validate_additional_groups(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        for g in v:
            _check_linux_id(g, "additional_groups")
        return v

    @property
    def effective_group(self) -> str:
        return self.group or self.username

    @property
    def effective_home(self) -> str:
        return self.home or f"/home/{self.username}"

    def apply(self, ctx: BuildContext) -> BuildContext:
        return ctx.updated(
            current_user=self.username,
            home=self.effective_home,
        ).with_extras(
            {
                "idegym.user.group": self.effective_group,
                "idegym.user.uid": self.uid,
                "idegym.user.gid": self.gid,
            }
        )

    def render(self, ctx: BuildContext) -> str:
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
    paths: dict[str, dict[str, Optional[str]]]

    @field_validator("paths")
    @classmethod
    def _validate_paths(cls, v: dict[str, dict[str, Optional[str]]]) -> dict[str, dict[str, Optional[str]]]:
        for config in v.values():
            for key in ("owner", "group"):
                val = config.get(key)
                if val is not None:
                    _check_linux_id(val, key)
            mode = config.get("mode")
            if mode is not None and not _OCTAL_MODE_RE.match(mode):
                raise ValueError(f"Invalid file mode: {mode!r}. Expected 3 or 4 octal digits (e.g. '755').")
        return v

    def render(self, ctx: BuildContext) -> str:
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


@image_plugin("project")
class Project(PluginBase):
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
            _check_linux_id(v, "owner/group")
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
            return ctx.updated(project_root=project_root)

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
        )

    def render(self, ctx: BuildContext) -> str:
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
            owner = self.owner or ctx.current_user
            group = self.group or owner
            commands = [
                f"mkdir -p {quote(ctx.project_root)}",
                f"curl -fsSL {quote(self.url)} -o /tmp/project-archive",
                f"extract /tmp/project-archive {quote(ctx.project_root)}",
                "rm -f /tmp/project-archive",
            ]
            if owner:
                commands.append(f"chown -R {owner}:{group} {quote(ctx.project_root)}")
            return _render_run_block(commands, comment="Download and extract project archive")

        if self.source == "git-clone":
            if self.url is None:
                raise ValueError("git-clone source requires a URL")
            owner = self.owner or ctx.current_user
            group = self.group or owner
            commands = [
                f"git clone {quote(self.url)} {quote(ctx.project_root)}",
            ]
            if self.ref and self.ref != "HEAD":
                commands.append(f"git -C {quote(ctx.project_root)} checkout {quote(self.ref)}")
            if owner:
                commands.append(f"chown -R {owner}:{group} {quote(ctx.project_root)}")
            return _render_run_block(commands, comment=f"Clone {self.url}")

        if ctx.request is None:
            raise ValueError("Project plugin must be applied before rendering")

        owner = self.owner or ctx.current_user
        group = self.group or owner
        commands = [
            f"mkdir -p {quote(ctx.project_root)}",
            "download $IDEGYM_PROJECT_ARCHIVE_URL $IDEGYM_PROJECT_ARCHIVE_PATH "
            "--auth-type ${IDEGYM_AUTH_TYPE:-} --auth-token ${IDEGYM_AUTH_TOKEN:-}",
            "extract $IDEGYM_PROJECT_ARCHIVE_PATH $IDEGYM_PROJECT_ROOT",
        ]
        if owner is not None:
            commands.append(f"chown -R {owner}:{group} {quote(ctx.project_root)}")

        return _render_run_block(commands, comment="Fetch and unpack the project")


def _idegym_server_env(home: str) -> str:
    return dedent(
        f"""\
        COPY --from=ghcr.io/astral-sh/uv:0.10.11 /uv /uvx /bin/

        ENV IDEGYM_PATH=/opt/idegym \\
            IDEGYM_PROJECT_ROOT={home}/work \\
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
    source: str
    root: Optional[str] = None
    url: Optional[str] = None
    ref: Optional[str] = None

    @classmethod
    def from_local(cls, root: Optional[Union[str, Path]] = None) -> "IdeGYMServer":
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
        return ctx.updated(context_path=self.root)

    def render(self, ctx: BuildContext) -> str:
        user = ctx.current_user
        group = str(ctx.get_extra("idegym.user.group", user))
        if self.source == "git":
            if self.url is None:
                raise ValueError("IdeGYMServer.from_git(...) requires a URL")
            return self._render_from_git(ctx, user, group)
        return self._render_from_local(ctx, user, group)

    def _render_from_git(self, ctx: BuildContext, user: str, group: str) -> str:
        ref = self.ref or "HEAD"
        clone_lines = [f"git clone {quote(self.url)} /tmp/idegym-src"]
        if ref != "HEAD":
            clone_lines.append(f"git -C /tmp/idegym-src checkout {quote(ref)}")
        clone_run = _render_run_block(clone_lines, comment=f"Clone IdeGYM from {self.url}")
        setup = dedent(
            f"""\
            RUN set -eux; \\
                mkdir -p $IDEGYM_PATH $IDEGYM_PROJECT_ROOT; \\
                cp -r /tmp/idegym-src/scripts/. /usr/local/bin/; \\
                cp /tmp/idegym-src/entrypoint.py $IDEGYM_PATH/; \\
                cp /tmp/idegym-src/entrypoint.sh /tmp/idegym-src/idegym.sh /usr/local/bin/; \\
                chmod 755 /usr/local/bin/* $IDEGYM_PATH/entrypoint.py; \\
                chown {user}:{group} /usr/local/bin/* $IDEGYM_PATH/entrypoint.py; \\
                for script in /usr/local/bin/*.{{py,sh}}; do \\
                    [ -f "$script" ] || continue; \\
                    mv "$script" "$(echo "${{script%.*}}" | tr "_" "-")"; \\
                done; \\
                cp /tmp/idegym-src/.python-version /tmp/idegym-src/pyproject.toml \\
                    /tmp/idegym-src/supervisord.conf /tmp/idegym-src/uv.lock $IDEGYM_PATH/; \\
                cp -r /tmp/idegym-src/api $IDEGYM_PATH/api; \\
                cp -r /tmp/idegym-src/backend-utils $IDEGYM_PATH/backend-utils; \\
                cp -r /tmp/idegym-src/common-utils $IDEGYM_PATH/common-utils; \\
                cp -r /tmp/idegym-src/rewards $IDEGYM_PATH/rewards; \\
                cp -r /tmp/idegym-src/tools $IDEGYM_PATH/tools; \\
                cp -r /tmp/idegym-src/server $IDEGYM_PATH/server; \\
                chown -R {user}:{group} $IDEGYM_PATH $IDEGYM_PROJECT_ROOT; \\
                rm -rf /tmp/idegym-src
            """
        ).rstrip()
        return "\n\n".join(
            [
                _idegym_server_env(ctx.home),
                clone_run,
                setup,
                f"USER {user}\nWORKDIR $IDEGYM_PATH",
                _idegym_server_uv_sync(),
                _idegym_server_tail(),
            ]
        )

    def _render_from_local(self, ctx: BuildContext, user: str, group: str) -> str:
        local_setup = dedent(
            f"""\
            RUN set -eux; \\
                mkdir -p $IDEGYM_PATH $IDEGYM_PROJECT_ROOT; \\
                chown -R {user}:{group} $IDEGYM_PATH $IDEGYM_PROJECT_ROOT

            COPY --chown={user}:{group} --chmod=755 scripts /usr/local/bin/
            COPY --chown={user}:{group} --chmod=755 entrypoint.py $IDEGYM_PATH/
            COPY --chown={user}:{group} --chmod=755 entrypoint.sh idegym.sh /usr/local/bin/

            RUN set -eux; \\
                for script in /usr/local/bin/*.{{py,sh}}; do \\
                    [ -f "$script" ] || continue; \\
                    mv "$script" "$(echo "${{script%.*}}" | tr "_" "-")"; \\
                done
            """
        ).rstrip()
        workspace_copies = dedent(
            f"""\
            COPY --chown={user}:{group} .python-version pyproject.toml supervisord.conf uv.lock ./
            COPY --chown={user}:{group} api api/
            COPY --chown={user}:{group} backend-utils backend-utils/
            COPY --chown={user}:{group} common-utils common-utils/
            COPY --chown={user}:{group} rewards rewards/
            COPY --chown={user}:{group} tools tools/
            COPY --chown={user}:{group} server server/
            """
        ).rstrip()
        return "\n\n".join(
            [
                _idegym_server_env(ctx.home),
                local_setup,
                f"USER {user}\nWORKDIR $IDEGYM_PATH",
                workspace_copies,
                _idegym_server_uv_sync(),
                _idegym_server_tail(),
            ]
        )


@image_plugin("pycharm")
class PyCharm(PluginBase):
    version: str = "2025.3"
    edition: str = "professional"
    user: Optional[str] = None

    @field_validator("version")
    @classmethod
    def _validate_version(cls, v: str) -> str:
        if not _PYCHARM_VERSION_RE.match(v):
            raise ValueError(f"Invalid PyCharm version: {v!r}. Expected format: YYYY.N or YYYY.N.N")
        return v

    @field_validator("edition")
    @classmethod
    def _validate_edition(cls, v: str) -> str:
        if v not in ("professional", "community"):
            raise ValueError(f"Invalid PyCharm edition: {v!r}. Must be 'professional' or 'community'.")
        return v

    @field_validator("user")
    @classmethod
    def _validate_user(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            _check_linux_id(v, "user")
        return v

    def render(self, ctx: BuildContext) -> str:
        user = self.user or ctx.current_user
        return dedent(
            f"""\
            # Install PyCharm {self.edition} {self.version}
            USER root
            RUN set -eux; \\
                apt-get update -qq; \\
                apt-get install -y --no-install-recommends \\
                    wget curl zip unzip \\
                    libxtst6 libxrender1 libxi6 libfreetype6 fontconfig; \\
                apt-get clean; \\
                rm -rf /var/lib/apt/lists/*

            # Install Java via SDKMAN (required for PyCharm)
            RUN curl -s "https://get.sdkman.io" | bash && \\
                bash -c "source /root/.sdkman/bin/sdkman-init.sh && sdk install java 21.0.5-tem"

            ENV JAVA_HOME="/root/.sdkman/candidates/java/current"
            ENV PATH="${{JAVA_HOME}}/bin:${{PATH}}"

            # Download and install PyCharm
            ENV PYCHARM_VERSION="{self.version}"
            ENV PYCHARM_DIR="/opt/pycharm"
            RUN wget -q "https://download.jetbrains.com/python/pycharm-{self.edition}-${{PYCHARM_VERSION}}.tar.gz" \\
                    -O /tmp/pycharm.tar.gz && \\
                mkdir -p ${{PYCHARM_DIR}} && \\
                tar -xzf /tmp/pycharm.tar.gz -C ${{PYCHARM_DIR}} --strip-components=1 && \\
                rm /tmp/pycharm.tar.gz

            ENV PATH="${{PYCHARM_DIR}}/bin:${{PATH}}"
            ENV DISPLAY=":99"

            USER {user}
            """
        ).strip()


__all__ = (
    "BuildContext",
    "BaseSystem",
    "IdeGYMServer",
    "Permissions",
    "PluginBase",
    "Project",
    "PyCharm",
    "User",
)
