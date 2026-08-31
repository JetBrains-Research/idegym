"""Normalization of a user-supplied Dockerfile used as an image's base.

An `idegym.image.builder.Image` can name its base either as a registry reference (``base``) or as
Dockerfile text (``base_dockerfile``). In the second form the user's stages are merged into the
same build as the plugin stages and the idegym stage, so no intermediate image is ever pushed.

Making that merge safe is what this module does. The user's text is emitted **verbatim except for
one edit**: the stage that acts as the base gains an ``AS`` alias the idegym stage can target. The
alias is only appended when the stage does not already declare one — renaming a user stage would
silently break their own ``COPY --from=`` references.

Parsing is delegated to `idegym.api.dockerfile_analysis`, which works at logical-line level so
continuations and heredocs do not confuse the scan.
"""

from dataclasses import dataclass
from typing import Optional

from idegym.api.dockerfile_analysis import (
    CopySource,
    SourceKind,
    copy_add_sources,
    parser_directives,
    stages,
    strip_parser_directives,
)

# Stage names beginning with this prefix are reserved for generated stages. Reserving a prefix
# rather than a single name covers both the base alias below and any stage a plugin emits.
RESERVED_STAGE_PREFIX = "idegym_"

# The alias given to the base stage when it does not already declare one.
BASE_STAGE_ALIAS = "idegym_base"

# The build arg the Cloud Build backend rewrites into a BuildKit secret mount. A user-side
# occurrence would be rewritten into a stage where no secret is mounted, so it is rejected.
AUTH_TOKEN_ARG = "IDEGYM_AUTH_TOKEN"


@dataclass(frozen=True, slots=True)
class NormalizedBase:
    """A ``base_dockerfile`` split into the pieces the renderer assembles.

    ``directives`` are hoisted to the very top of the merged file, where Docker will still read
    them. ``body`` is the user's stages with the alias applied. ``alias`` is the ``FROM`` target
    the idegym stage uses, and the value `idegym.api.plugin.BuildContext.base` carries.
    """

    directives: tuple[str, ...]
    body: str
    alias: str


def normalize_base_dockerfile(content: str, base_stage: Optional[str] = None) -> NormalizedBase:
    """Prepare ``content`` for merging, resolving and aliasing the stage that acts as the base.

    ``base_stage`` selects which stage of a multi-stage input is the base; it defaults to the last
    one, matching what ``docker build`` would produce from the file on its own.

    Raises ``ValueError`` for input that cannot work: no ``FROM`` at all, a ``base_stage`` naming a
    stage that is not declared, or a user stage inside the reserved ``idegym_`` namespace.
    """
    directives = tuple(parser_directives(content))
    body = strip_parser_directives(content)
    declared = stages(body)

    if not declared:
        raise ValueError("'base_dockerfile' contains no FROM instruction, so it declares no base image")

    reserved = [stage.alias for stage in declared if stage.alias and stage.alias.startswith(RESERVED_STAGE_PREFIX)]
    if reserved:
        raise ValueError(
            f"Stage name(s) {', '.join(sorted(reserved))} use the reserved '{RESERVED_STAGE_PREFIX}' prefix. "
            "That namespace belongs to generated stages; rename them."
        )

    if base_stage is None:
        target = declared[-1]
    else:
        # Docker treats stage names case-insensitively, so the lookup does too.
        matches = [stage for stage in declared if stage.alias and stage.alias.lower() == base_stage.lower()]
        if not matches:
            named = [stage.alias for stage in declared if stage.alias]
            available = ", ".join(named) if named else "none — no stage declares an 'AS <name>' alias"
            raise ValueError(
                f"base_stage '{base_stage}' is not a stage of 'base_dockerfile'. Declared stages: {available}"
            )
        target = matches[-1]

    if target.alias:
        return NormalizedBase(directives=directives, body=body.strip(), alias=target.alias)

    lines = body.splitlines()
    lines[target.line.end] = f"{lines[target.line.end].rstrip()} AS {BASE_STAGE_ALIAS}"
    return NormalizedBase(directives=directives, body="\n".join(lines).strip(), alias=BASE_STAGE_ALIAS)


def local_context_sources(content: str) -> list[CopySource]:
    """Return the ``COPY``/``ADD`` sources that read from the build context.

    Non-empty with no context supplied means the build cannot succeed, so the caller rejects it up
    front rather than letting Kaniko or BuildKit fail halfway through. ``COPY --from=`` and
    ``ADD <url>`` are excluded — they need no context.
    """
    return [source for source in copy_add_sources(content) if source.kind is SourceKind.LOCAL]


def references_auth_token(content: str) -> bool:
    """Whether ``content`` mentions the reserved `AUTH_TOKEN_ARG` build arg."""
    return AUTH_TOKEN_ARG in content
