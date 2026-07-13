"""External IDE plugin sources and their Dockerfile rendering.

IntelliJ-based IDEs load any plugin dropped into ``${IDE_DIR}/plugins`` at startup.
mcp-steroid is installed this way (download a ZIP, unzip into the plugins dir); this
module generalises that mechanism so ``Idea`` and ``PyCharm`` can bake an arbitrary,
ordered list of extra plugins from the same code path.

Private downloads are supported without leaking the credential: a ``PluginSource`` names
a build-time environment variable via ``auth_env``. The variable is consumed inside a
``RUN`` as a build ``ARG`` and injected into an ``Authorization`` header; it is never
turned into an ``ENV`` (so it does not persist in an image layer) and the ``curl`` line is
kept out of the build log by disabling shell tracing around it. Keeping the token out of
an image layer relies on a BuildKit or Kaniko backend (a build ``ARG`` is not baked into
the image); the classic pre-BuildKit Docker builder would record ``ARG`` values in
``docker history``. Put credentials in ``auth_env``, never in the ``url`` — the URL is
emitted verbatim into the Dockerfile.
"""

import re
from shlex import quote
from typing import Optional
from urllib.parse import urlsplit

from idegym.api.type import AuthType, HttpUrl
from pydantic import BaseModel, ConfigDict, field_validator

# A POSIX-ish environment variable / Docker build-arg name. Matched with ``fullmatch`` so a
# trailing newline (which ``$`` would tolerate) cannot slip into the emitted Dockerfile.
_ENV_VAR_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class PluginSource(BaseModel):
    """A single external IDE plugin to install into ``${IDE_DIR}/plugins`` at build time.

    The plugin is downloaded as a ZIP archive from ``url`` and unzipped into the IDE's
    bundled plugins directory, exactly the way mcp-steroid is installed.

    Attributes:
        url: HTTP(S) URL of the plugin ZIP. The URL path must end with ``.zip``.
        auth_env: Optional name of a build-time environment variable holding the download
            credential. When set, the value is forwarded as a build ``ARG`` of the same
            name and sent as an ``Authorization`` header. The secret is not baked into the
            image and is kept out of the build log. Must be a valid env-var name.
        auth_scheme: The ``Authorization`` header scheme used with ``auth_env``. One of
            ``Bearer`` (default), ``Token`` or ``Basic`` (the value is used verbatim, so
            ``Basic`` expects a pre-encoded ``user:pass`` token).
    """

    url: HttpUrl
    auth_env: Optional[str] = None
    auth_scheme: AuthType = "Bearer"

    model_config = ConfigDict(frozen=True, extra="forbid")

    @field_validator("url")
    @classmethod
    def _validate_url(cls, v: str) -> str:
        if not urlsplit(v).path.lower().endswith(".zip"):
            raise ValueError(f"Plugin URL must point to a .zip archive: {v!r}")
        return v

    @field_validator("auth_env")
    @classmethod
    def _validate_auth_env(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not _ENV_VAR_RE.fullmatch(v):
            raise ValueError(
                f"Invalid auth_env {v!r}. Must be a valid environment variable name "
                r"matching ^[A-Za-z_][A-Za-z0-9_]*$"
            )
        return v


def external_plugin_build_secrets(sources: tuple[PluginSource, ...]) -> list[str]:
    """Return the unique ``auth_env`` names required by ``sources``, preserving order.

    These are the build-arg names the build backend must source from its environment so
    the authenticated downloads succeed. See ``PluginBase.get_build_secrets``.
    """
    names: list[str] = []
    for source in sources:
        if source.auth_env is not None and source.auth_env not in names:
            names.append(source.auth_env)
    return names


def _render_source(source: PluginSource, index: int, plugins_dir: str) -> str:
    archive = f"/tmp/external-plugin-{index}.zip"
    # ``plugins_dir`` holds a shell variable (``${IDE_DIR}/plugins``) that must expand at
    # build time, so it is double-quoted (never shell-quoted) — matching the mcp-steroid
    # template. ``unzip -d`` creates the (already-present) plugins dir, so no ``mkdir``.
    into = f'"{plugins_dir}"'

    if source.auth_env is None:
        curl = f"curl -fsSL {quote(source.url)} -o {archive}"
        comment = f"# Install external plugin: {source.url}"
        arg_line = ""
    else:
        # Reference the credential as a build ARG (never an ENV, so it is not baked into a
        # layer) and inject it as an Authorization header.
        missing_msg = f"{source.auth_env} build arg is required for authenticated plugin download"
        header = f"Authorization: {source.auth_scheme} ${{{source.auth_env}:?{missing_msg}}}"
        curl = f'curl -fsSL -H "{header}" {quote(source.url)} -o {archive}'
        comment = f"# Install external plugin (authenticated via ${source.auth_env}): {source.url}"
        arg_line = f"ARG {source.auth_env}\n"

    # Disable shell tracing around curl so neither the Authorization header nor any secret
    # accidentally embedded in the URL is echoed to the build log by ``set -x``. The
    # ``2>/dev/null`` on the ``set +x`` group also suppresses the trace of the toggle itself.
    commands = [
        "{ set +x; } 2>/dev/null",
        curl,
        "{ set -x; } 2>/dev/null",
        f"unzip -qo {archive} -d {into}/",
        f"rm -f {archive}",
    ]
    body = " && \\\n    ".join(commands)
    return f"{comment}\n{arg_line}RUN set -eux; \\\n    {body}"


def render_external_plugins(sources: tuple[PluginSource, ...], *, plugins_dir: str) -> str:
    """Return Dockerfile fragments installing ``sources`` into ``plugins_dir``, in order.

    Emits one ``RUN`` block per source. Returns an empty string when ``sources`` is empty.
    The install must run while the build user is ``root`` (``plugins_dir`` is IDE-owned),
    which is where ``Idea``/``PyCharm`` place it in their ``render()`` pipeline.
    """
    return "\n\n".join(_render_source(source, index, plugins_dir) for index, source in enumerate(sources))
