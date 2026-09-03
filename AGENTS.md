# AGENTS.md

Working agreements for the IdeGYM repository — the conventions a new contributor would
otherwise pick up by osmosis over a few review cycles. Written for both humans and coding
agents.

**Precedence:** the code and the config files are the source of truth. If this document
disagrees with `pyproject.toml`, `.pre-commit-config.yaml`, or a workflow, the code wins —
and the mismatch is a bug in this file. See [Keeping this file true](#keeping-this-file-true).

**Scope:** durable rules and invariants only. Feature-level internals live next to the code
(package `README.md` files) or in [`website/docs/`](website/docs/); this file links to them
rather than duplicating them.

---

## 1. Orientation

IdeGYM creates disposable, scalable development environments on Kubernetes. A Python
monorepo (Python 3.12, `uv` workspace) split into packages that mirror the runtime topology:

| Package | Role | Published to PyPI |
|---|---|---|
| `api/` | Pydantic models and shared types — the contract between every other package | yes |
| `common-utils/` | Small dependency-light helpers (logging, hashing, dict, path) | yes |
| `client/` | Python client for the orchestrator and in-pod server | yes |
| `image-builder/` | `Image` fluent API, spec compilation, local Docker driver | yes |
| `plugins/` | Image / server / client plugins (defaults, idea, pycharm, openhands) | yes |
| `orchestrator/` | FastAPI control plane: servers, builds, snapshots, DB, migrations | no — image only |
| `server/` | FastAPI service that runs inside each environment pod | no — image only |
| `watcher/` | Background reconciler: cleanup, crash detection | no — image only |
| `backend-utils/` | Kubernetes / Docker / image-build backends used by the services | no — image only |
| `tools/`, `rewards/` | In-pod tool execution and reward computation | no — image only |

Which packages publish is enforced by the `exclude` list in
[`.github/scripts/build-packages.sh`](.github/scripts/build-packages.sh) — a new package
defaults to *published* unless you add it there.

Dependency direction matters and is easy to break:

- `common-utils` imports nothing of ours; `api` imports only `common-utils`. Everything
  else may import both. Keep their third-party dependency lists small — every service pays
  for anything added there.
- `backend-utils` depends on `api` and **not** on `image-builder`. So logic a build backend
  needs — anything under `backend-utils/.../image_builder/` — has to live in `common-utils/`,
  `api/`, or `backend-utils` itself, never beside the `Image` model. Prefer `common-utils` when
  it carries no models: `idegym.utils.dockerfile` is pure text analysis, shared by both backends
  and by `image-builder`'s base-Dockerfile normalization.
- `watcher` depends on `orchestrator` deliberately, for the shared database layer and
  process setup. It is the one service-to-service dependency; do not add more.
- `server` runs in a *different image* from the control plane and must never import
  `orchestrator`, `watcher`, or anything that reaches Kubernetes on the control-plane side.
  Its in-pod dependencies are `api`, `common-utils`, `backend-utils`, `tools`, `rewards`,
  and `plugins`.

---

## 2. Environment

Prerequisites: [`uv`](https://github.com/astral-sh/uv) >= 0.10, Docker (integration tests
and local image builds), Python 3.12 (installed by `uv`).

```bash
uv sync --frozen --all-packages --all-extras --all-groups
pre-commit install
```

- **Use `--frozen` locally.** A plain `uv sync` (or a bare `uv run`, which syncs implicitly)
  may re-resolve and rewrite `uv.lock` as a side effect of unrelated work. CI syncs
  unfrozen on purpose; in a feature branch an incidental lockfile diff is noise. Check with
  `uv lock --check`, and revert `uv.lock` if you did not mean to change dependencies.
- **Everything runs through `uv run`.** Never a bare `python`, `pip`, or `pytest` — the
  workspace packages are only importable inside the managed venv.
- A fresh checkout or worktree needs its own `uv sync`; otherwise imports fail with
  `ModuleNotFoundError: No module named 'idegym'`.

### Commands

| Purpose | Command |
|---|---|
| Lint | `uv run ruff check` |
| Autofix (safe fixes only) | `uv run ruff check --fix` |
| Format | `uv run ruff format` |
| Unit tests | `uv run pytest -m unit` |
| Integration tests | `IDEGYM_TEST_REGISTRY=localhost:5000 uv run pytest -m integration` |
| E2E tests (needs Minikube) | `uv run pytest -m e2e` |
| All hooks | `pre-commit run --all-files` |
| Docs site | `cd website && npm ci && npm run build` |

---

## 3. Python coding standards

1. **`Optional[X]`, not `X | None`.** Non-optional generics use the builtins: `dict[str, Any]`,
   `list[str]`. The `UP045` ignore in `pyproject.toml` stops ruff rewriting `Optional[X]`, but
   nothing enforces the choice mechanically, so a handful of `X | None` annotations do exist
   in the sources. They are drift, not precedent — the house style is `Optional[X]`.
2. **Annotate public functions** — parameters and return type. Internal helpers may omit
   annotations only when the type is obvious from a one-line body.
3. **Absolute imports everywhere.** There are zero relative imports in the package sources;
   keep it that way. Note that `idegym.*` sorts *inside the third-party block* (there is no
   `known-first-party` config), so `from idegym.api...` lands between `fastapi` and
   `pydantic`. Do not hand-fix import order — run `uv run ruff check --fix`.
4. **`api/` owns the wire contract.** Request/response models, enums, and shared type
   aliases go in `api/`, never in a service package. Keep `api/`'s own dependencies minimal:
   everything imports it.
5. **Configuration is nested Pydantic models** in `api/src/idegym/api/config.py`, composed
   into `Config`. Add a field with `Field(default=...)` and a `description=` when the name
   is not self-explanatory; do not read `os.environ` ad hoc in service code. Inherit
   `ConfigModel` and set `env_segment`: every leaf field is then settable at deploy time as
   `IDEGYM_<env_segment>_<FIELD_NAME>`, generated by `ConfigModel`, so a new field needs no
   alias of its own. Reach for `env()` only for the exceptions — `env(name=...)` for a
   variable another library owns, `env(legacy=[...])` to keep a pre-rename name working —
   and never write `validation_alias` by hand; `ConfigModel` overwrites it. Renaming a field
   or a segment renames a deployment's variables: add the old name to `legacy`, then update
   both the frozen `ENVIRONMENT_VARIABLES` table in `unit-tests/test_settings.py` — the one
   place the contract is written out in full — and the `LEGACY_SAMPLES` entry beside it that
   proves the old name still resolves. A legacy name that supplies a value is logged as
   deprecated at startup, so retire it once deployments have moved. Add
   `validate_default=True` when a validator normalises the value, or the default bypasses it.
   *Adding* a field means two tables in that same test file, not one: `ENVIRONMENT_VARIABLES`
   for its variable name and `HYDRA_DEFAULTS` for its default, which is compared as an exact
   dict — a new field with no entry there fails the suite rather than being ignored. A
   `Duration` field is set from the environment as an ISO-8601 duration (`PT10M`) or a
   `H:MM:SS` string; a bare number of seconds is rejected. See
   `backend-utils/src/idegym/backend/utils/settings.py`.
6. **Logging goes through structlog:**
   ```python
   from idegym.utils.logging import get_logger

   logger = get_logger(__name__)
   ```
   Bind context as keyword arguments (`logger.info("Server started", server=name)`) rather
   than formatting into the message. Never log tokens, passwords, or auth headers.
7. **Exceptions inherit from `IdeGYMException`** (`api/src/idegym/api/exceptions.py`).
   Routers translate them into HTTP status codes; see
   [HTTP Error Codes](website/docs/reference/http_error_codes.md).
8. **Docstrings are prose, not parameter tables.** The house style is a short paragraph
   explaining *why* something exists and what invariant it holds, with ``double backticks``
   around code references. Google-style `Args:`/`Returns:` blocks are the exception, used
   only where an argument list genuinely needs one. The same applies to comments: explain
   the rationale, not the mechanics.
9. **Formatting is not negotiable and not manual:** 120 columns, 4-space indent, double
   quotes, LF endings, magic trailing commas respected. `uv run ruff format` decides.
10. **Watch out for type aliases that are called at runtime.** `Duration: TypeAlias = timedelta`
    is instantiated as `Duration(seconds=30)`. A PEP 695 `type` statement is not callable,
    which is why `UP040` is ignored. Same class of hazard for any alias used as a value.

### The lint contract

The project runs ruff's **full default rule set** plus `I` (isort). Three rules are opted
out — `UP045`, `UP040`, `PYI041` — each with a comment in `pyproject.toml` explaining why.

- **Adding an ignore requires a written reason** in the same place. An unexplained entry in
  `ignore` or `per-file-ignores` will be asked about in review.
- **Per-file relaxations apply only to `*-tests/**`, `examples/**`, and `scripts/**`.**
  Library code keeps full enforcement — do not widen a glob to silence a finding in `src`.
- **`ruff check --fix` (safe fixes) is fine; `--unsafe-fixes` is not, unattended.** Unsafe
  fixes can rewrite a runtime-evaluated module-level alias into something that raises at
  import. After any bulk fix, run `uv run pytest -m unit` — collection alone catches
  import-time regressions.
- **Markdown is formatted too.** Ruff formats Python inside fenced code blocks in `.md`
  files, so a snippet in a README is held to the same style as source. The pre-commit hook
  is scoped to `\.py$` and will not catch it — a repo-wide `uv run ruff format` will.
- **Nothing checks formatting in CI.** The lint workflow runs `ruff check` only, never
  `ruff format --check`. Format drift is caught by the pre-commit hook, or not at all — so
  run `uv run ruff format` before you push, and install the hooks.

---

## 4. Testing rules

Three suites, one directory each: `unit-tests/`, `integration-tests/`, `e2e-tests/`.

- **Markers are assigned by directory**, automatically, in the root
  [`conftest.py`](conftest.py) — a test in `unit-tests/` is `-m unit` without any decorator.
  Do not hand-add `@pytest.mark.unit`.
- **E2E is deselected by default.** `uv run pytest` with no `-m` runs unit + integration
  only; e2e requires an explicit marker expression.
- **Test files must be named `test_*.py`** — enforced by the `name-tests-test` pre-commit
  hook (helper directories `utils/`, `config/`, `test_projects/` are exempt).
- **Test order is randomized** (`pytest-randomly`). No test may depend on another having run
  first, and shared fixtures must clean up after themselves.
- **Suite boundaries:**
  - `unit` — no cluster, no network, no daemon. Kubernetes and Docker clients are mocked;
    the whole suite runs in about a minute and is the one you run constantly.
  - `integration` — real Docker: builds images, runs containers, needs a local registry
    (`IDEGYM_TEST_REGISTRY`). Database tests live here, against an ephemeral PostgreSQL
    started by `testcontainers[postgres]` (`integration-tests/database/`), so `~` regex,
    `FOR UPDATE SKIP LOCKED`, and advisory locks behave as they do in production.
  - `e2e` — a real Minikube cluster; minutes per test.
- **Push coverage down.** Anything expressible as a pure function belongs in `unit-tests/`.
  Reserve e2e for behaviour that genuinely requires a cluster.
- **New e2e file? Pick its CI group.** E2E runs as a six-way matrix
  (`idea | pycharm | openhands | kaniko | mcp | other`) keyed off `E2E_GROUP_BY_FILE_PREFIX`
  in the root `conftest.py`. An unlisted file falls into the `other` catch-all, so nothing
  is silently dropped — but if your file is slow, add a prefix entry so the groups stay
  balanced.

---

## 5. Playbooks for changes that bite

These are the invariants that most often cost a review round-trip. Consult the matching one
before you start.

### Adding a field to a server operation

Request fields plumb through **five layers**, and a partial change compiles fine while
silently dropping the value:

1. `client/src/idegym/client/client.py` — the user-facing method
2. `client/src/idegym/client/operations/servers.py` — the operation wrapper
3. `api/src/idegym/api/orchestrator/servers.py` — the request model
4. `orchestrator/src/idegym/orchestrator/router/server.py` — the handler
5. `backend-utils/src/idegym/backend/utils/kubernetes_client.py` — where it reaches Kubernetes

Then ask: does the new field change what a *reused* or *restored* server looks like? If so
it belongs in `_HASH_FIELDS`
(`orchestrator/src/idegym/orchestrator/snapshot_pipeline.py`); if it is purely runtime, it
deliberately stays out. Getting this wrong means either a snapshot that is silently reused
with the wrong configuration, or one that never matches and is rebuilt every time.

A handler that needs `Config` reads it from `low_level_request.app.state.config` and delegates
to a `<name>_with_config` twin holding the actual logic. The MCP tools in
`orchestrator/src/idegym/orchestrator/mcp.py` call these functions directly rather than over
HTTP, so they have no `Request` to inject — a handler that reaches for the config itself is
unreachable from MCP. `start_server` / `start_server_with_config` is the model.

### Database migrations

Read [`orchestrator/src/idegym/orchestrator/migrations/README.md`](orchestrator/src/idegym/orchestrator/migrations/README.md)
first — `alembic.ini` and `migrations/` both live under
`orchestrator/src/idegym/orchestrator/`. Beyond that README:

- **A migration is a four-file change.** Ship `<rev>_<slug>.py`, `<rev>_up.sql`,
  `<rev>_down.sql`, and the matching `database.schemaRevision` bump in
  `charts/idegym/values.yaml` — plus the SQLAlchemy model in
  `orchestrator/src/idegym/orchestrator/database/models.py`. A column that exists in only one
  of model and migration is the classic bug; a stale `schemaRevision` makes the orchestrator
  refuse to start, since it is the exact revision a rollback downgrades to.
- **Downgrades are executed, not decorative.** `scripts/rollback.py` runs
  `<rev>_down.sql` to roll a release's schema back, so a down migration that only ever
  compiled is a deployment hazard. Never create or drop `alembic_version` in a migration:
  Alembic updates it in the same transaction, so touching it aborts the downgrade.
- **Verify there is exactly one head** before opening the PR:
  ```bash
  uv run alembic -c orchestrator/src/idegym/orchestrator/alembic.ini heads
  ```
  Sequential three-digit revision IDs collide easily when two branches are open at once. If
  you see two heads, renumber yours — do not merge them. Exact-revision rollback needs the
  history linear, so a merge revision is rejected outright.
- **Run the round-trip suite**, which drives every revision up and down against a real
  PostgreSQL container with data seeded at each step:
  ```bash
  uv run pytest integration-tests/database/test_migration_roundtrip.py
  ```
  It also asserts the head schema matches the ORM models — the rest of the DB suite builds its
  schema with `Base.metadata.create_all` and would not notice the drift.

The rollback workflow itself is documented in
[`website/docs/reference/database_rollback.md`](website/docs/reference/database_rollback.md).

### Adding or changing a plugin

Plugins are discovered through entry points, not imports. See
[Plugin Architecture](website/docs/reference/plugins.md).

- Register under the right group in `plugins/pyproject.toml`:
  `idegym.plugins.image`, `idegym.plugins.server`, or `idegym.plugins.client`.
- A new plugin source root must be added to **both** `[tool.hatch.build.targets.wheel]
  packages` **and** `dev-mode-dirs`. Miss `dev-mode-dirs` and the package imports fine from
  a wheel but is invisible in an editable install — so it works in CI and breaks locally.
- Assets that live outside `src/` (scripts, zips) need a `force-include` entry and must be
  read via `plugin_asset()`, never a path relative to `__file__`.
- The keys returned by `get_context_files()` must match the `COPY` paths in the plugin's
  Dockerfile template; a unit test enforces this.

### Anything that changes a built image

`ImageBuildSpec.image_version()` hashes the inputs that define an image. If your change
affects the produced image but is not folded into that hash, builds will reuse a stale
cached image and the change will appear not to work. Add the new input to `image_version()`.

Two rules about *how* to add it:

- **Append it conditionally, and label it.** An identifier added unconditionally changes every
  existing hash, so every deployed image rebuilds once. Guard it (`if self.context_uri is not
  None:`) so definitions that do not use the field keep the tag they already have. The
  identifiers are concatenated without a separator, so prefix new ones (`f"context_uri={...}"`)
  to keep the concatenation unambiguous.
- **Hash secret *names*, never values.** `secret_build_args` and `secrets` contribute only their
  names: a rotated credential behind the same id produces the same image, so hashing the value
  or version would be pure rebuild churn — and would put a secret into everything derived from
  the spec.

Inputs that do **not** belong in the hash: the destination registry/tag, and build resources
(timeout, machine type, disk size). None of them change image content.

### Build secrets

- Declare them via `PluginBase.get_build_secrets()`; they are passed as `--build-arg`,
  never as `ENV` (an `ENV` persists in the image layer).
- There are **two** mechanisms and they are not interchangeable: `secret_build_args` (the above —
  names resolved from the orchestrator's own environment) and `ImageBuildSpec.secrets`
  (caller-supplied Secret Manager resource names). The second is read from a *mount* on
  `cloudbuild_gke` and from a build arg on `kaniko`, so the two backends need **different
  Dockerfiles** and the Kaniko form exposes the value in the image history. Before touching either,
  read [build secrets across backends](website/docs/reference/image_builder.md).
- A model field that names a secret holds a *reference*, never a value. `secrets` enforces the
  Secret Manager resource-name shape for this reason: the spec is serialized into a build request
  and a job record.
- Wrap the consuming command so `set -x` cannot echo it:
  ```bash
  { set +x; } 2>/dev/null
  <command that reads the secret>
  { set -x; } 2>/dev/null
  ```
- Never interpolate a credential into a URL in a Dockerfile — the URL is stored verbatim.

### CI, lint, and dependency configuration

- **CI runs on a pull request whatever its base**, so a stacked pull request based on another
  feature branch is still checked. The six **e2e groups are the exception**: they only run when
  the base is `main` (or on a push to `main`), because each provisions its own cluster. A stack
  therefore gets lint, unit and integration per layer, and full e2e as each layer's base merges
  and GitHub retargets it onto `main`. Do not add a `branches:` filter back to `pull_request`.
- **A CI check's name is `<workflow name> / <job name>`.** Renaming a workflow or job
  invalidates branch-protection required checks. If your PR renames one, say so explicitly
  in the description so the repo settings get updated.
- Dependency update policy lives in [`.github/dependabot.yml`](.github/dependabot.yml):
  routine updates are grouped and quarterly, semver-majors are ignored (human-initiated),
  security updates are alert-driven. Bump policy there rather than opening one-off PRs.
- Server base images (Debian/Ubuntu tags) are **not** Dependabot-trackable — they live in a
  Python dict in `scripts/build_server_images.py` and are bumped by hand.

### Documentation

User-facing docs live in [`website/docs/`](website/docs/) and are published to GitHub Pages.

- **`npm run build` is the gate.** `onBrokenLinks`, `onBrokenAnchors`, and
  `onBrokenMarkdownLinks` are all `throw`, so one dead relative link or `#anchor` fails the
  docs job. Validate with `cd website && npm ci && npm run build`.
- Site links are baseUrl-relative (`/architecture/...`); links to repository source must be
  full `https://github.com/JetBrains-Research/idegym/blob/main/...` URLs.
- Mermaid `click` directives are the exception — they emit a raw `<a href>` with no baseUrl
  applied, so they need the full `/idegym/...` prefix.
- `website/docs/reference/` is the developer reference documentation; `architecture/`,
  `overview/`, and `deployment.md` are the presentation layer. **Feature PRs tend to update
  only `reference/` and leave the presentation pages stale** — if your change alters
  architecture, update both.

---

## 6. Git and pull requests

- **Branches** are namespaced: `<your-name>/<short-slug>`.
- **Commit subjects** are `[area] Imperative summary` — e.g. `[orchestrator] Add snapshot
  retention policy`. The area tag is the package, subsystem, or concern (`ci`, `helm`,
  `docs`, `lint`, `e2e`). PRs are squash-merged, so the subject becomes the changelog entry
  and gets `(#N)` appended automatically.
- **PR titles** carry the tracked issue's ID in square brackets, separated from the title by
  a single space. The PR template names the exact prefix and carries a matching `Resolves:`
  line — keep both pointing at the same issue.
- **Keep the diff in scope.** Drive-by reformatting, unrelated renames, and opportunistic
  `uv.lock` churn make a review much more expensive. Find something unrelated and broken?
  Note it in the PR description rather than fixing it in the same change.
- Releases are tag-driven; see [`RELEASING.md`](RELEASING.md) and
  [`CHANGELOG.md`](CHANGELOG.md).

---

## 7. Keeping this file true

This file is only worth reading if it is accurate, and it decays silently. Treat it as part
of the change, not as documentation about the change.

**Update it in the same PR when you:**

- add, remove, or reinterpret a lint rule, ignore, or per-file exception;
- change how tests are selected, grouped, or run (markers, suites, CI matrix);
- change the workspace layout — a new package, a new plugin group, a new publish target;
- introduce or discover an invariant that is not visible from the code you touched — the
  five-layer plumbing chain and the `dev-mode-dirs` trap are the model for what belongs;
- hit a failure that cost you a debugging session or a CI round-trip, where the fix is not
  obvious from the error. Write down the symptom *and* the cause — the symptom is what the
  next person will search for.

**Write it as current state.** Describe how things *are*, never how they changed: "`alembic.ini`
lives under `orchestrator/src/idegym/orchestrator/`", not "`alembic.ini` moved from X to Y".
A reader needs today's rule, and before/after framing accumulates into a changelog nobody
can act on. Edit the existing sentence rather than appending a correction next to it.

**Do not add:** anything already obvious from the code, ticket-specific status, a narration
of what one PR did, or a summary of a feature that belongs in that feature's `README.md`.
Prefer a link to a package README over a paragraph here. If a section stops being true,
delete it — a stale rule is worse than a missing one.

**For coding agents specifically:** read this file before planning, not after failing. When
you learn something that would have saved you time, add it here before you finish, keeping
the existing tone and structure. Fold new knowledge into the section where it belongs rather
than appending a new one, and prune whatever your change made false.
