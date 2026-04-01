from dataclasses import dataclass, field
from pathlib import Path
from shlex import quote
from textwrap import dedent
from typing import Any, ClassVar

from idegym.api.download import Authorization, DownloadRequest
from idegym.api.git import GitRepository, GitRepositoryResource, GitRepositorySnapshot
from idegym.api.type import AuthType
from idegym.image.plugin import BuildContext, Plugin, PluginBase, image_plugin


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
    auth: Authorization | None,
    auth_type: AuthType | None,
    auth_token: str | None,
) -> Authorization:
    if auth is not None and (auth_type is not None or auth_token is not None):
        raise ValueError("Use either 'auth' or 'auth_type'/'auth_token', not both")
    if auth is not None:
        return auth
    return Authorization(type=auth_type, token=auth_token)


def _authorization_to_payload(auth: Authorization) -> dict[str, Any]:
    return {
        "type": auth.type,
        "token": auth.token,
    }


def _authorization_from_payload(payload: dict[str, Any] | None) -> Authorization:
    if payload is None:
        return Authorization()
    return Authorization(
        type=payload.get("type"),
        token=payload.get("token"),
    )


@image_plugin("base-system")
@dataclass(frozen=True, slots=True)
class BaseSystem(PluginBase):
    DEFAULT_PACKAGES: ClassVar[tuple[str, ...]] = (
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

    packages: tuple[str, ...] = field(default_factory=lambda: BaseSystem.DEFAULT_PACKAGES)

    def to_payload(self) -> dict[str, Any]:
        return {
            "packages": list(self.packages),
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "BaseSystem":
        packages = payload.get("packages")
        if packages is None:
            return cls()
        return cls(packages=tuple(packages))

    def render(self, ctx: BuildContext) -> str:
        if not self.packages:
            return ""
        package_list = " \\\n".join(f"                    {package}" for package in self.packages)
        # language=dockerfile
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
                fc-cache -f -v || true; \\
                update-ca-certificates
            """
        ).strip()


@image_plugin("user")
@dataclass(frozen=True, slots=True)
class User(PluginBase):
    username: str
    uid: int = 1000
    gid: int = 1000
    group: str | None = None
    home: str | None = None
    shell: str = "/bin/bash"
    sudo: bool = True
    create_home: bool = True
    additional_groups: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        return {
            "username": self.username,
            "uid": self.uid,
            "gid": self.gid,
            "group": self.group,
            "home": self.home,
            "shell": self.shell,
            "sudo": self.sudo,
            "create_home": self.create_home,
            "additional_groups": list(self.additional_groups),
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "User":
        username = payload.get("username")
        return cls(
            username=username,
            uid=payload.get("uid", 1000),
            gid=payload.get("gid", 1000),
            group=payload.get("group"),
            home=payload.get("home"),
            shell=payload.get("shell", "/bin/bash"),
            sudo=payload.get("sudo", True),
            create_home=payload.get("create_home", True),
            additional_groups=tuple(payload.get("additional_groups", [])),
        )

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
@dataclass(frozen=True, slots=True)
class Permissions(PluginBase):
    paths: dict[str, dict[str, str | None]]

    def to_payload(self) -> dict[str, Any]:
        return {"paths": self.paths}

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "Permissions":
        paths = payload.get("paths", {})
        return cls(paths={path: dict(config) for path, config in paths.items()})

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
@dataclass(frozen=True, slots=True)
class Project(PluginBase):
    source: str
    url: str
    ref: str = "HEAD"
    path: str | None = None
    auth: Authorization = field(default_factory=Authorization)
    target: str | None = None
    owner: str | None = None
    group: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "url": self.url,
            "ref": self.ref,
            "path": self.path,
            "auth": _authorization_to_payload(self.auth),
            "target": self.target,
            "owner": self.owner,
            "group": self.group,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "Project":
        return cls(
            source=payload.get("source"),
            url=payload.get("url"),
            ref=payload.get("ref", "HEAD"),
            path=payload.get("path"),
            auth=_authorization_from_payload(payload.get("auth")),
            target=payload.get("target"),
            owner=payload.get("owner"),
            group=payload.get("group"),
        )

    @classmethod
    def from_git(
        cls,
        *,
        url: str,
        ref: str = "HEAD",
        auth: Authorization | None = None,
        auth_type: AuthType | None = None,
        auth_token: str | None = None,
        target: str | None = None,
        owner: str | None = None,
        group: str | None = None,
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
        auth: Authorization | None = None,
        auth_type: AuthType | None = None,
        auth_token: str | None = None,
        target: str | None = None,
        owner: str | None = None,
        group: str | None = None,
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


@image_plugin("idegym-server")
@dataclass(frozen=True, slots=True)
class IdeGYMServer(PluginBase):
    source: str
    root: str | None = None
    url: str | None = None
    ref: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "root": self.root,
            "url": self.url,
            "ref": self.ref,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "IdeGYMServer":
        return cls(
            source=payload.get("source"),
            root=payload.get("root"),
            url=payload.get("url"),
            ref=payload.get("ref"),
        )

    @classmethod
    def from_local(cls, root: str | Path | None = None) -> "IdeGYMServer":
        root_path = Path.cwd() if root is None else Path(root)
        return cls(source="local", root=str(root_path.expanduser().resolve()))

    @classmethod
    def from_git(cls, *, url: str, ref: str = "HEAD") -> "IdeGYMServer":
        return cls(source="git", url=url, ref=ref)

    def apply(self, ctx: BuildContext) -> BuildContext:
        if self.source == "git":
            raise NotImplementedError("IdeGYMServer.from_git(...) is not implemented yet")
        if self.root is None:
            raise ValueError("IdeGYMServer.from_local(...) requires a workspace root")
        return ctx.updated(context_path=self.root)

    def render(self, ctx: BuildContext) -> str:
        if self.source == "git":
            raise NotImplementedError("IdeGYMServer.from_git(...) is not implemented yet")

        user = ctx.current_user
        group = str(ctx.get_extra("idegym.user.group", user))
        # language=dockerfile
        return dedent(
            f"""\
            COPY --from=ghcr.io/astral-sh/uv:0.10.11 /uv /uvx /bin/

            ENV IDEGYM_PATH=/opt/idegym \\
                PYTHONDONTWRITEBYTECODE=0 \\
                PYTHONUNBUFFERED=1 \\
                PYTHONHASHSEED=random
            ENV PYTHONPATH="$IDEGYM_PATH"

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

            USER {user}
            WORKDIR $IDEGYM_PATH

            COPY --chown={user}:{group} .python-version pyproject.toml supervisord.conf uv.lock ./
            COPY --chown={user}:{group} api api/
            COPY --chown={user}:{group} backend-utils backend-utils/
            COPY --chown={user}:{group} common-utils common-utils/
            COPY --chown={user}:{group} rewards rewards/
            COPY --chown={user}:{group} tools tools/
            COPY --chown={user}:{group} server server/

            RUN set -eux; \\
                uv python install; \\
                uv sync --project server \\
                    --frozen \\
                    --no-cache \\
                    --no-dev; \\
                uv pip install supervisor

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
        ).strip()


__all__ = (
    "BuildContext",
    "BaseSystem",
    "IdeGYMServer",
    "Permissions",
    "Plugin",
    "PluginBase",
    "Project",
    "User",
)
