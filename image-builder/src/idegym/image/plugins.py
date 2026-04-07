from pathlib import Path
from shlex import quote
from textwrap import dedent
from typing import ClassVar, Optional, Union

from idegym.api.download import Authorization, DownloadRequest
from idegym.api.git import GitRepository, GitRepositoryResource, GitRepositorySnapshot
from idegym.api.type import AuthType
from idegym.image.plugin import BuildContext, PluginBase, image_plugin
from pydantic import Field


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
        "fontconfig",
        "git",
        "netcat-openbsd",
        "sudo",
    )

    packages: tuple[str, ...] = DEFAULT_PACKAGES

    def render(self, ctx: BuildContext) -> str:
        if not self.packages:
            return ""
        package_list = " \\\n".join(f"                    {package}" for package in self.packages)
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
    url: str
    ref: str = "HEAD"
    path: Optional[str] = None
    auth: Authorization = Field(default_factory=Authorization)
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
            raise NotImplementedError("IdeGYMServer.from_git(...) is not implemented yet")
        if self.root is None:
            raise ValueError("IdeGYMServer.from_local(...) requires a workspace root")
        return ctx.updated(context_path=self.root)

    def render(self, ctx: BuildContext) -> str:
        if self.source == "git":
            raise NotImplementedError("IdeGYMServer.from_git(...) is not implemented yet")

        user = ctx.current_user
        group = str(ctx.get_extra("idegym.user.group", user))
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
    "PluginBase",
    "Project",
    "User",
)
