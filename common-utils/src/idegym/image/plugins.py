from dataclasses import dataclass, field
from pathlib import Path
from shlex import quote
from typing import ClassVar, Mapping, Optional

from idegym.api.download import Authorization, DownloadRequest
from idegym.api.git import GitRepository, GitRepositoryResource, GitRepositorySnapshot
from idegym.api.type import AuthType

from .plugin import BuildContext, Plugin, PluginBase

DEFAULT_BASE_SYSTEM_PACKAGES = (
    "bash",
    "ca-certificates",
    "coreutils",
    "curl",
    "dumb-init",
    "findutils",
    "fontconfig",
    "git",
    "netcat-openbsd",
    "sudo",
)


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


def _render_run_block(commands: list[str], *, comment: str | None = None) -> str:
    filtered = [command.strip() for command in commands if command.strip()]
    if not filtered:
        return ""
    body = " && \\\n    ".join(filtered)
    prefix = f"# {comment}\n" if comment else ""
    return f"{prefix}RUN set -eux; \\\n    {body}"


def _build_authorization(
    auth: Optional[Authorization],
    auth_type: Optional[AuthType],
    auth_token: Optional[str],
) -> Authorization:
    if auth is not None and (auth_type is not None or auth_token is not None):
        raise ValueError("Use either 'auth' or 'auth_type'/'auth_token', not both")
    if auth is not None:
        return auth
    return Authorization(type=auth_type, token=auth_token)


@dataclass(frozen=True, slots=True)
class BaseSystem(PluginBase):
    packages: tuple[str, ...] = field(default_factory=lambda: DEFAULT_BASE_SYSTEM_PACKAGES)

    def render(self, ctx: BuildContext) -> str:
        if not self.packages:
            return ""
        package_list = " \\\n        ".join(self.packages)
        return "\n".join(
            [
                "# Install base system packages",
                "RUN set -eux; \\",
                "    apt-get update -qq; \\",
                "    apt-get install -y --no-install-recommends \\",
                f"        {package_list}; \\",
                "    apt-get clean; \\",
                "    rm -rf /var/lib/apt/lists/*",
                "",
                "# Refresh system caches used by IDE tools",
                "RUN set -eux; \\",
                "    fc-cache -f -v || true; \\",
                "    update-ca-certificates",
            ]
        )


@dataclass(frozen=True, slots=True)
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

        if additional_groups:
            group_flags = f"-G {additional_groups}"
        else:
            group_flags = ""

        commands.append(
            "if id -u {username} >/dev/null 2>&1; then "
            "usermod -u {uid} -g {group} -d {home} -s {shell} {username}; "
            "{add_groups}; "
            "else "
            "useradd -u {uid} -g {group} {group_flags} -d {home} -s {shell} {create_home} {username}; "
            "fi".format(
                username=self.username,
                uid=self.uid,
                group=group,
                home=quote(home),
                shell=quote(self.shell),
                add_groups=f"usermod -aG {additional_groups} {self.username}" if additional_groups else ":",
                group_flags=group_flags,
                create_home=create_home_flag,
            )
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


@dataclass(frozen=True, slots=True)
class Permissions(PluginBase):
    paths: Mapping[str, Mapping[str, str | None]]

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


@dataclass(frozen=True, slots=True)
class Project(PluginBase):
    source: str
    url: str
    ref: str = "HEAD"
    path: Optional[str] = None
    auth: Authorization = field(default_factory=Authorization)
    target: Optional[str] = None
    owner: Optional[str] = None
    group: Optional[str] = None

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
            auth=_build_authorization(auth, auth_type, auth_token),
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
        return cls(
            source="resource",
            url=url,
            ref=ref,
            path=path,
            auth=_build_authorization(auth, auth_type, auth_token),
            target=target,
            owner=owner,
            group=group,
        )

    def project(self) -> GitRepositorySnapshot | GitRepositoryResource:
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
        if ctx.request is None:
            raise ValueError("Project plugin must be applied before rendering")

        owner = self.owner or ctx.current_user
        group = self.group or owner
        commands = [
            f"mkdir -p {quote(ctx.project_root)}",
            "download $IDEGYM_PROJECT_ARCHIVE_URL $IDEGYM_PROJECT_ARCHIVE_PATH "
            "--auth-type $IDEGYM_AUTH_TYPE --auth-token $IDEGYM_AUTH_TOKEN",
            "extract $IDEGYM_PROJECT_ARCHIVE_PATH $IDEGYM_PROJECT_ROOT",
        ]
        if owner is not None:
            commands.append(f"chown -R {owner}:{group} {quote(ctx.project_root)}")

        return _render_run_block(commands, comment="Fetch and unpack the project")


@dataclass(frozen=True, slots=True)
class IdegymServer(PluginBase):
    UV_IMAGE: ClassVar[str] = "ghcr.io/astral-sh/uv:0.10.11"
    WORKSPACE_FILES: ClassVar[tuple[str, ...]] = (".python-version", "pyproject.toml", "supervisord.conf", "uv.lock")
    WORKSPACE_DIRS: ClassVar[tuple[str, ...]] = ("api", "backend-utils", "common-utils", "rewards", "tools", "server")

    source: str
    root: Optional[str] = None
    url: Optional[str] = None
    ref: Optional[str] = None

    @classmethod
    def from_local(cls, root: Optional[str | Path] = None) -> "IdegymServer":
        root_path = Path.cwd() if root is None else Path(root)
        return cls(source="local", root=str(root_path.expanduser().resolve()))

    @classmethod
    def from_git(cls, *, url: str, ref: str = "HEAD") -> "IdegymServer":
        return cls(source="git", url=url, ref=ref)

    def apply(self, ctx: BuildContext) -> BuildContext:
        if self.source == "git":
            raise NotImplementedError("IdegymServer.from_git(...) is not implemented yet")
        if self.root is None:
            raise ValueError("IdegymServer.from_local(...) requires a workspace root")
        return ctx.updated(context_path=self.root)

    def render(self, ctx: BuildContext) -> str:
        if self.source == "git":
            raise NotImplementedError("IdegymServer.from_git(...) is not implemented yet")

        user = ctx.current_user
        group = str(ctx.get_extra("idegym.user.group", user))

        return "\n".join(
            [
                f"COPY --from={self.UV_IMAGE} /uv /uvx /bin/",
                "",
                "ENV IDEGYM_PATH=/opt/idegym \\",
                "    PYTHONDONTWRITEBYTECODE=0 \\",
                "    PYTHONUNBUFFERED=1 \\",
                "    PYTHONHASHSEED=random",
                'ENV PYTHONPATH="$IDEGYM_PATH"',
                "",
                "RUN set -eux; \\",
                "    mkdir -p $IDEGYM_PATH $IDEGYM_PROJECT_ROOT; \\",
                f"    chown -R {user}:{group} $IDEGYM_PATH $IDEGYM_PROJECT_ROOT",
                "",
                f"COPY --chown={user}:{group} --chmod=755 scripts /usr/local/bin/",
                f"COPY --chown={user}:{group} --chmod=755 entrypoint.py $IDEGYM_PATH/",
                f"COPY --chown={user}:{group} --chmod=755 entrypoint.sh idegym.sh /usr/local/bin/",
                "",
                "RUN set -eux; \\",
                "    for script in /usr/local/bin/*.{py,sh}; do \\",
                '        [ -f "$script" ] || continue; \\',
                '        mv "$script" "$(echo "${script%.*}" | tr "_" "-")"; \\',
                "    done",
                "",
                f"USER {user}",
                "WORKDIR $IDEGYM_PATH",
                "",
                f"COPY --chown={user}:{group} {' '.join(self.WORKSPACE_FILES)} ./",
                *(f"COPY --chown={user}:{group} {path} {path}/" for path in self.WORKSPACE_DIRS),
                "",
                "RUN set -eux; \\",
                "    uv python install; \\",
                "    uv sync --project server \\",
                "            --frozen \\",
                "            --no-cache \\",
                "            --no-dev; \\",
                "    uv pip install supervisor",
                "",
                "VOLUME /docker-entrypoint.d",
                "EXPOSE 8000",
                "",
                'ENTRYPOINT ["dumb-init", "--"]',
                'CMD ["entrypoint", ".venv/bin/supervisord", "-c", "supervisord.conf"]',
                "",
                "HEALTHCHECK \\",
                "    --start-period=10s \\",
                "    --interval=60s \\",
                "    --timeout=30s \\",
                "    --retries=5 \\",
                "CMD nc -z 127.0.0.1 8000 || exit 1",
            ]
        )


__all__ = [
    "BuildContext",
    "BaseSystem",
    "DEFAULT_BASE_SYSTEM_PACKAGES",
    "IdegymServer",
    "Permissions",
    "Plugin",
    "PluginBase",
    "Project",
    "User",
]
