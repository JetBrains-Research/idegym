"""Normalization of a user-supplied Dockerfile used as an image's base.

When an `idegym.image.builder.Image` gives its base as Dockerfile text, the user's stages are
merged into the same build as the plugin and idegym stages, so no intermediate image is pushed.
Making that merge safe is the job here: the text is emitted verbatim apart from one edit, an ``AS``
alias on the stage acting as the base, and only when it does not already declare one — renaming a
user stage would break their own ``COPY --from=`` references.

Parsing is delegated to `idegym.utils.dockerfile`.
"""

from dataclasses import dataclass
from typing import Optional

from idegym.utils.dockerfile import (
    CopySource,
    copy_add_sources,
    declared_instructions,
    escape_character,
    logical_lines,
    parser_directives,
    stages,
    strip_parser_directives,
)

# Reserved for generated stages — a prefix rather than a name, to cover plugin stages too.
RESERVED_STAGE_PREFIX = "idegym_"

# The alias given to the base stage when it does not already declare one.
BASE_STAGE_ALIAS = "idegym_base"

# The build arg the Cloud Build backend rewrites into a BuildKit secret mount, so a user-side
# occurrence would be rewritten into a stage where no secret is mounted.
AUTH_TOKEN_ARG = "IDEGYM_AUTH_TOKEN"

# Instructions the generated stage sets for itself. See `overridden_instruction_warning`.
OVERRIDABLE_INSTRUCTIONS = ("ENTRYPOINT", "CMD", "HEALTHCHECK")


@dataclass(frozen=True, slots=True)
class NormalizedBase:
    """A ``base_dockerfile`` split into the pieces the renderer assembles.

    ``directives`` are hoisted to the top of the merged file, ``body`` is the user's stages with the
    alias applied, and ``alias`` is the ``FROM`` target the idegym stage uses.
    """

    directives: tuple[str, ...]
    body: str
    alias: str


def normalize_base_dockerfile(content: str, base_stage: Optional[str] = None) -> NormalizedBase:
    """Prepare ``content`` for merging, resolving and aliasing the stage that acts as the base.

    ``base_stage`` selects which stage of a multi-stage input is the base, defaulting to the last
    one, as ``docker build`` would. Raises ``ValueError`` for input that cannot work: no ``FROM``, an
    undeclared ``base_stage``, or a user stage in the reserved ``idegym_`` namespace.
    """
    directives = tuple(parser_directives(content))
    body = strip_parser_directives(content)
    # Read from the *original* text: an `# escape=` directive is one of the lines just removed, so
    # scanning the body alone would fall back to a backslash and join the wrong lines.
    escape = escape_character(content)
    declared = stages(body, escape=escape)

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
    front instead of letting the backend fail halfway through. ``COPY --from=`` and ``ADD <url>``
    are excluded — they need no context.
    """
    return [source for source in copy_add_sources(content) if source.reads_build_context]


def overridden_instruction_warning(base_dockerfile: str, generated: str) -> Optional[str]:
    """Warn when the base declares runtime instructions the generated stage replaces.

    ``FROM <alias>`` inherits ``ENTRYPOINT``, ``CMD`` and ``HEALTHCHECK``, but a plugin declaring its
    own — ``idegym-server`` emits all three — renders after the primary ``FROM`` and overrides them,
    leaving the base's version dead. A warning rather than a rejection: plenty of bases carry an
    ``ENTRYPOINT`` nobody depends on, but one that performs real setup loses it silently.
    """
    declared = declared_instructions(base_dockerfile, OVERRIDABLE_INSTRUCTIONS)
    if not declared:
        return None

    overridden = sorted(set(declared) & set(declared_instructions(generated, OVERRIDABLE_INSTRUCTIONS)))
    if not overridden:
        return None

    listed = ", ".join(f"{name} (line {declared[name].number})" for name in overridden)
    return (
        f"'base_dockerfile' declares {listed}, which the generated idegym stage replaces, so the base's "
        "version never takes effect. Setup that lives in an ENTRYPOINT will not run. Move it into a "
        "build-time RUN so the image is self-contained, or install a script into /docker-entrypoint.d/ "
        "to have it run before the server starts."
    )


def references_auth_token(content: str) -> bool:
    """Whether an *instruction* in ``content`` mentions the reserved `AUTH_TOKEN_ARG` build arg.

    Logical lines rather than raw text, so a comment naming the arg is not a rejection: only a real
    reference would be rewritten by the Cloud Build backend.
    """
    return any(AUTH_TOKEN_ARG in line.text for line in logical_lines(content))
