# Image Builder

IdeGYM's image builder lets you compose Docker images for environment containers using a **plugin-based API**.
Instead of writing raw Dockerfiles, you describe what an environment needs — system packages, users, projects,
IDEs — and the builder assembles the Dockerfile from reusable, validated building blocks.

Images can be defined in Python (fluent API) or YAML, and can be built locally with Docker or inside the
cluster with Kaniko.

## Table of Contents

- [Architecture](#architecture)
- [Python Fluent API](#python-fluent-api)
- [YAML Format](#yaml-format)
- [Built-in Plugins](#built-in-plugins)
  - [base-system](#base-system)
  - [user](#user)
  - [permissions](#permissions)
  - [project](#project)
  - [idegym-server](#idegym-server)
  - [pycharm](#pycharm)
- [Building Images](#building-images)
  - [Local Docker build](#local-docker-build)
  - [Kaniko build (in-cluster)](#kaniko-build-in-cluster)
  - [Build backends](#build-backends)
  - [GKE Cloud Build backend](#gke-cloud-build-backend)
  - [Backend capability matrix](#backend-capability-matrix)
  - [Build secrets across backends](#build-secrets-across-backends)
  - [Build cost and deduplication](#build-cost-and-deduplication)
- [Writing Custom Plugins](#writing-custom-plugins)

---

## Architecture

An `Image` is an immutable description of a Docker image. It consists of:

- A **base**, given either as an image reference (`base`) or as Dockerfile text compiled in the same
  build (`base_dockerfile`)
- An ordered list of **plugins** — each plugin modifies a shared `BuildContext` and emits a Dockerfile fragment
- Optional **shell commands** appended after all plugin fragments
- Runtime configuration (Kubernetes runtime class, resource requests/limits)

When you call `image.to_spec()` (or `image.build()`), the builder:

1. Normalizes `base_dockerfile`, if given: the stage acting as the base is aliased so the generated
   stage can target it, and parser directives are hoisted
2. Creates a `BuildContext` whose `base` is that alias (or the `base` reference), with defaults
   (`current_user="root"`, `home="/root"`, `project_root="/root/work"`)
3. Iterates through plugins in order; each plugin:
   - `apply(ctx)` — updates the context (e.g., sets `current_user` after creating a user)
   - `render(ctx)` — returns a Dockerfile fragment string
4. Assembles the final Dockerfile: hoisted parser directives, the user's own stages, plugin build
   stages, then the `FROM` clause, optional ARGs for downloads, `ENV` declarations, all plugin
   fragments, the final `USER` line, and the commands block

```
Image.to_spec()
  ├─ normalize base_dockerfile → directives, aliased stages, base alias
  ├─ BuildContext(base=<alias or reference>, current_user="root", home="/root", ...)
  ├─ plugin[0].apply(ctx) → new ctx
  ├─ plugin[0].render(ctx) → Dockerfile fragment
  ├─ plugin[1].apply(ctx) → new ctx
  ├─ plugin[1].render(ctx) → Dockerfile fragment
  └─ assemble Dockerfile → ImageBuildSpec
```

Everything from the primary `FROM` onwards is generated identically whichever base form was used,
which is what makes switching an existing definition to an inline base produce an equivalent image.

The resulting `ImageBuildSpec` contains the complete `dockerfile_content` string and any associated
metadata (download request, labels, platforms, runtime config, build context reference, build args,
secret names, destination overrides).

---

## Python Fluent API

The `Image` class provides a chainable API. Every method returns a new `Image` (the class is immutable).

### Import

```python
from idegym.image.builder import Image
from idegym.plugins.defaults.image import BaseSystem, User, Permissions, Project, IdeGYMServer
```

### `Image.from_base(base)`

Create an image from a base image reference:

```python
image = Image.from_base("ghcr.io/jetbrains-research/idegym/server-debian-bookworm-20250520-slim:latest")
```

### `Image.from_dockerfile(content)` / `Image.from_dockerfile_path(path)`

Create an image whose base is **an inline Dockerfile compiled in the same build**, so a custom base
needs no separate build-and-push cycle:

```python
image = Image.from_dockerfile(
    """
    FROM debian:bookworm-slim AS builder
    RUN apt-get update && apt-get install -y build-essential
    FROM debian:bookworm-slim
    COPY --from=builder /usr/bin/foo /usr/bin/foo
    """,
    name="my-env",
)
```

`Image.from_dockerfile_path("path/to/Dockerfile")` is the same thing, reading the file **immediately**
— the content is inlined at authoring time. It has to be: the orchestrator receives only
`yaml_content` as a string and has no access to your filesystem, so a path would be unresolvable by
the time the build runs.

Exactly one of `base` and `base_dockerfile` may be set. Pass `base_stage=` to nominate which stage of
a multi-stage Dockerfile acts as the base; the default is the last one, matching what `docker build`
would produce from the file on its own.

**How the merge works.** The stage acting as the base gains an `AS idegym_base` alias — and only if
it does not already declare one, since renaming your stage would break your own `COPY --from=`
references. Your stages are then emitted first, followed by any plugin build stages, followed by the
generated idegym stage:

```dockerfile
FROM debian:bookworm-slim AS builder
RUN apt-get update && apt-get install -y build-essential
FROM debian:bookworm-slim AS idegym_base      # ← alias added
COPY --from=builder /usr/bin/foo /usr/bin/foo
# --- plugin build stages, unchanged ---
FROM idegym_base                              # ← the generated stage
SHELL ["/bin/bash", "-c"]
USER root
...
```

`FROM idegym_base` inherits that stage's full image config — `ENV`, `WORKDIR`, `USER`, `ENTRYPOINT`,
`CMD` — which is what makes this **equivalent** to publishing the base and referencing it by tag. A
test asserts the generated segment is byte-identical between the two forms.

:::warning Your ENTRYPOINT, CMD and HEALTHCHECK do not survive
Inheritance happens at the `FROM`, but plugin fragments render *after* it, so a plugin that declares
its own wins. The `idegym-server` plugin declares **all three** — it has to, since it owns how the
container starts.

The consequence is easy to miss and expensive: a base whose `ENTRYPOINT` performs the real setup —
cloning a repository, warming a build cache — loses it, the container starts against an empty
working directory, and **nothing reports an error**. This is not specific to an inline base; a
pre-published `base:` behaves identically. It simply bites far more often here, because bringing
your own image is the whole point.

Compiling such a definition records a warning on the build's job record, so it is visible from
`/api/jobs/status/{job_name}` after the fact rather than only in an orchestrator log.

Two ways to keep the setup:

- **Move it into a build-time `RUN`** (recommended). The image becomes self-contained, the work
  happens once at build time instead of on every container start, and no network is needed at run
  time. For a Dockerfile that writes its setup as a heredoc script and then invokes it via
  `ENTRYPOINT`, this is a mechanical rewrite.
- **Install a script into `/docker-entrypoint.d/`.** The image's entrypoint runs every `*.sh` there
  to completion before starting the server, and a non-zero exit fails the container. Three caveats:
  the glob is **not sorted**, so ship exactly one script; output is captured and only logged when
  the script finishes, so a long step appears to hang; and `exec "$@"` must be stripped, since the
  script is run directly rather than as an entrypoint wrapper.
:::

Rejected up front, rather than as a build failure minutes later:

| Input | Why |
|---|---|
| No `FROM` instruction | Declares no base image |
| `base_stage` naming an undeclared stage | Error lists the stages that do exist |
| A stage named `idegym_*` | Reserved for generated stages |
| A reference to `IDEGYM_AUTH_TOKEN` | The Cloud Build backend rewrites that name into a secret mount on the generated stage, so a user-side occurrence would be rewritten into a stage where no secret is mounted |
| `COPY`/`ADD` from the build context with no `context_uri` | Nothing to copy from. `COPY --from=<stage>` and `ADD <url>` need no context and are always fine |

### `.with_context(context_uri)`

Point the build at a context archive you have already staged, so an inline Dockerfile's `COPY`/`ADD`
resolve:

```python
image = Image.from_dockerfile(dockerfile_text).with_context("gs://my-bucket/contexts/abc123.tar.gz")
```

The orchestrator never receives context bytes over the API — you stage a `tar` (optionally gzipped)
somewhere the build backend can read, and pass its URI. The field is deliberately opaque about the
scheme, because which schemes work is a property of the configured backend:

| Scheme | `kaniko` | `cloudbuild_gke` |
|---|---|---|
| `gs://` | yes | yes |
| `s3://`, `https://`, `git://` | yes | no — `StorageSource` is GCS-only |

:::warning Name the object by its content
The image tag is derived from the **URI**, not from the bytes the backend later fetches. Reusing one
object name for changed contents therefore reads as an unchanged image and you get a stale cache hit.
Use a content-addressed object name.
:::

### `.with_secrets(**mapping)`

Map a Dockerfile secret id to a **Secret Manager resource name** — never a value:

```python
image = image.with_secrets(gh_token="projects/my-project/secrets/gh-token/versions/latest")
```

Only names travel in the definition; the value is resolved at build time. **How it reaches the build
differs by backend, and so does the Dockerfile you must write** — see
[Build secrets across backends](#build-secrets-across-backends).

### `.with_destination(tag=... | registry=..., version=...)`

Push somewhere other than the orchestrator's default registry:

```python
image = image.with_destination(tag="europe-west1-docker.pkg.dev/my-project/my-repo/env:abc123")
```

Checked against the deployment's `build.allowed_registry_prefixes` allowlist, which is **empty by
default** — an arbitrary destination means pushing anywhere the builder's service account can write,
so a deployment has to opt in. Supplying only `version` keeps the default registry and merely opts
you out of hash-based deduplication.

### `.with_build_resources(timeout_seconds=..., machine_type=..., disk_size_gb=...)`

Ask for more build capacity than the deployment default:

```python
image = image.with_build_resources(timeout_seconds=5400, disk_size_gb=500)
```

Timeouts and disk sizes are clamped to configured ceilings; `machine_type` is checked against an
allowlist and **refused** if absent from it, rather than silently downgraded. `machine_type` and
`disk_size_gb` are Cloud Build only — a Kaniko build runs in a pod, so its lever is the per-image
`resources` field set by [`.with_runtime()`](#with_runtimeruntime_class_name-resources). Kaniko
records them as ignored on the job rather than letting the request look honoured.

### `.named(name)`

Assign a name used as the output image tag:

```python
image = image.named("my-environment")
```

### `.with_plugin(plugin)`

Append a plugin:

```python
image = image.with_plugin(User(username="appuser", uid=1000, gid=1000))
```

### `.run_commands(*commands)`

Append shell commands (run as `RUN set -eux; ...`). These execute after all plugin fragments, as the
user set by the last `User` plugin (or `root` if no `User` plugin was used):

```python
image = image.run_commands(
    "echo 'hello' > /home/appuser/hello.txt",
    "pip install numpy pandas",
)
```

> [!NOTE]
> `commands` are plain shell commands — do **not** add the `RUN` prefix. The builder adds
> `RUN set -eux;` automatically and joins commands with ` && \`.

### `.pip_install(*packages)`

Convenience shorthand for `run_commands("pip install ...")`:

```python
image = image.pip_install("numpy", "pandas", "scikit-learn")
```

### `.with_platforms(*platforms)`

Set target platforms for multi-arch builds:

```python
image = image.with_platforms("linux/amd64", "linux/arm64")
```

### `.with_runtime(runtime_class_name, resources)`

Set Kubernetes runtime configuration (used when deploying as a server):

```python
image = image.with_runtime(
    runtime_class_name="gvisor",
    resources={
        "requests": {"cpu": "500m", "memory": "512Mi", "ephemeral-storage": "2Gi"},
        "limits": {"cpu": "1", "memory": "1Gi", "ephemeral-storage": "2Gi"},
    },
)
```

### Chaining example

```python
from idegym.image.builder import Image
from idegym.plugins.defaults.image import BaseSystem, User, Project, Permissions

image = (
    Image.from_base("ghcr.io/jetbrains-research/idegym/server-debian-bookworm-20250520-slim:latest")
    .named("my-python-env")
    .with_plugin(BaseSystem(packages=("ca-certificates", "curl", "git", "python3")))
    .with_plugin(User(username="devuser", uid=2000, gid=2000, sudo=True))
    .with_plugin(
        Project.from_git(
            url="https://github.com/your-org/your-repo.git",
            ref="main",
            owner="devuser",
            target="/home/devuser/project",
        )
    )
    .with_plugin(Permissions(paths={"/home/devuser/project": {"owner": "devuser", "mode": "755"}}))
    .pip_install("pytest", "black")
    .with_runtime(
        runtime_class_name="gvisor",
        resources={
            "requests": {"cpu": "500m", "memory": "512Mi"},
            "limits": {"cpu": "1", "memory": "1Gi"},
        },
    )
)
```

---

## YAML Format

Images can be defined declaratively in YAML, which is useful for configuration-driven workflows and
for submitting build jobs to the orchestrator.

### Structure

```yaml
images:
  - base: <base-image>        # Full Docker image reference. Exactly one of
                              # `base` / `base_dockerfile` is required.
    base_dockerfile: |        # Inline Dockerfile compiled in the same build
      FROM debian:bookworm-slim
      ...
    base_stage: <stage-name>  # Optional: which stage of base_dockerfile is the
                              # base. Default: the last one.
    context_uri: gs://...     # Optional: pre-staged build context archive
    name: <output-tag>        # Optional: tag for the built image
    plugins:                  # Optional: list of plugins
      - type: <plugin-type>
        <plugin-fields>: ...
    commands:                 # Optional: shell commands (no RUN prefix)
      - echo "hello"
    secrets:                  # Optional: secret id -> Secret Manager resource name
      gh_token: projects/p/secrets/gh-token/versions/latest
    tag: <registry>/<repo>:<v>  # Optional: full destination. Excludes registry/version.
    registry: <registry>        # Optional: destination registry prefix
    version: <version>          # Optional: destination version. Default: content hash.
    timeout_seconds: 5400       # Optional: per-build timeout, clamped
    machine_type: E2_HIGHCPU_8  # Optional: Cloud Build only, allowlisted
    disk_size_gb: 500           # Optional: Cloud Build only, clamped
    runtime_class_name: gvisor  # Default: gvisor
    resources:                  # Optional: Kubernetes resource spec
      requests:
        cpu: "500m"
        memory: "512Mi"
      limits:
        cpu: "1"
        memory: "1Gi"
```

### Inline Dockerfile base

`base_dockerfile` takes a YAML block scalar, and round-trips as one — `to_yaml()` emits multiline
strings with `|`, so a definition loaded and re-dumped is unchanged:

```yaml
images:
  - name: my-env
    base_dockerfile: |
      FROM debian:bookworm-slim AS builder
      RUN apt-get update && apt-get install -y build-essential
      FROM debian:bookworm-slim
      COPY --from=builder /usr/bin/foo /usr/bin/foo
      COPY setup.sh /usr/local/bin/setup.sh
    context_uri: gs://my-bucket/contexts/abc123.tar.gz
    plugins:
      - type: base-system
      - type: idea
        headless: true
```

See [`Image.from_dockerfile`](#imagefrom_dockerfilecontent--imagefrom_dockerfile_pathpath) for how the
merge works and what is rejected.

Multiple images can be defined in a single file:

```yaml
images:
  - base: debian:bookworm-20250520-slim
    plugins:
      - type: base-system
    commands:
      - echo "image-1"

  - base: debian:bookworm-20250520-slim
    plugins:
      - type: base-system
    commands:
      - echo "image-2"
```

### Load from YAML in Python

```python
# Single image
image = Image.from_yaml(yaml_string)

# Multiple images
images = Image.load_all(yaml_string)

# From a file path
with open("image.yaml") as f:
    image = Image.from_yaml(f.read())
```

### Serialize to YAML

```python
yaml_str = image.to_yaml()
path = image.write_yaml("image.yaml")
```

---

## Built-in Plugins

### `base-system`

Installs system packages via `apt-get`. Use this as the first plugin when starting from a plain Debian/Ubuntu base.

**Python:**
```python
from idegym.plugins.defaults.image import BaseSystem

# Default packages (bash, ca-certificates, curl, dumb-init, findutils, git, netcat-openbsd, sudo)
BaseSystem()

# Custom packages
BaseSystem(packages=("ca-certificates", "curl", "git", "jq", "vim"))

# Minimal: only ca-certificates and curl (useful for scratch-like images)
BaseSystem(minimal=True)
```

**YAML:**
```yaml
- type: base-system
  packages:
    - ca-certificates
    - curl
    - git
    - jq
```

**Default packages:**
`bash`, `ca-certificates`, `coreutils`, `curl`, `dumb-init`, `findutils`, `git`, `netcat-openbsd`, `sudo`

Package names must be valid Debian package names (`^[a-z0-9][a-z0-9+.-]+$`).

---

### `user`

Creates a Linux user and group in the container. After this plugin runs, `ctx.current_user` and `ctx.home`
are updated to the new user, so subsequent plugins and commands run in the correct context.

**Python:**
```python
from idegym.plugins.defaults.image import User

User(
    username="appuser",
    uid=1000,
    gid=1000,
    group="appuser",  # Optional primary group name (defaults to username)
    home="/home/appuser",  # Optional, defaults to /home/<username>
    shell="/bin/bash",  # Optional, defaults to /bin/bash
    sudo=True,  # Grant passwordless sudo
    additional_groups=("docker",),  # Optional extra groups to join
)
```

**YAML:**
```yaml
- type: user
  username: appuser
  uid: 1000
  gid: 1000
  sudo: true
  additional_groups:
    - docker
```

**Notes:**
- `group` sets the primary group name (created with the same GID). Defaults to the username.
- `additional_groups` adds the user to supplementary groups (groups must already exist or the plugin will create them).
- If the user already exists (by name), the plugin updates UID, home, and shell — idempotent.
- The plugin always runs as `USER root` internally and leaves `ctx.current_user` set to the new user.

---

### `permissions`

Sets file/directory ownership and mode (chmod). Useful for fixing ownership after copying files or
creating directories.

**Python:**
```python
from idegym.plugins.defaults.image import Permissions

Permissions(
    paths={
        "/home/appuser": {"owner": "appuser", "mode": "755"},
        "/home/appuser/.config": {"owner": "appuser"},  # mode optional
        "/var/log/app": {"mode": "777"},  # owner optional
    }
)
```

**YAML:**
```yaml
- type: permissions
  paths:
    /home/appuser:
      owner: appuser
      mode: "755"
    /var/log/app:
      mode: "777"
```

**Notes:**
- `mode` must be a 3- or 4-digit octal string (e.g., `"755"`, `"0755"`)
- `owner` sets both user and group ownership (`chown owner:owner`)
- Both `owner` and `mode` are optional, but at least one must be specified per path

---

### `project`

Loads a project into the container image. Five sources are supported:

| Source | Method | How it works |
|--------|--------|-------------|
| `git` | `from_git()` | Downloads a git repo snapshot as an archive via IdeGYM's download infrastructure; extracts at build time |
| `resource` | `from_resource()` | Downloads a single file from a git repo via IdeGYM's download infrastructure |
| `local` | `from_local()` | Emits a Docker `COPY` from the build context — no network access |
| `archive` | `from_archive()` | Downloads a direct archive URL with `curl` and extracts it; no git required |
| `git-clone` | `from_git_clone()` | Runs `git clone` + `git checkout`; requires `git` to be installed (provided by `base-system`) |

#### `from_git` — via IdeGYM download infrastructure

Downloads the repository as an archive using IdeGYM's download/extract scripts. The archive URL and
optional auth token are injected as Docker ARGs so they are evaluated at build time (Kaniko passes them
via `--build-arg`). Use this for pinned commits and private repos with token auth.

```python
from idegym.plugins.defaults.image import Project

Project.from_git(
    url="https://github.com/your-org/your-repo.git",
    ref="abc1234",  # branch, tag, or commit SHA (pin to a SHA for reproducibility)
    owner="appuser",  # file ownership inside the container
    target="/home/appuser/work",  # destination path (defaults to $HOME/work)
    group="appuser",  # optional group (defaults to owner)
    auth=None,  # optional Authorization for private repos
)
```

```yaml
- type: project
  source: git
  url: https://github.com/your-org/your-repo.git
  ref: abc1234
  owner: appuser
  target: /home/appuser/work
```

#### `from_resource` — single file from a git repo

Downloads one specific file from a git repository via IdeGYM's download infrastructure.

```python
Project.from_resource(
    url="https://github.com/your-org/your-repo.git",
    ref="abc1234",
    path="scripts/setup.sh",  # path to the file inside the repo
    owner="appuser",
    target="/home/appuser/work",
)
```

```yaml
- type: project
  source: resource
  url: https://github.com/your-org/your-repo.git
  ref: abc1234
  path: scripts/setup.sh
  owner: appuser
  target: /home/appuser/work
```

#### `from_local` — copy from build context

Emits a `COPY` instruction. No network access; the directory must be present in the Docker build context.

```python
Project.from_local(
    path="./my-project",  # path relative to the build context
    target="/home/appuser/work",
    owner="appuser",
    group="appuser",
)
```

```yaml
- type: project
  source: local
  path: ./my-project
  target: /home/appuser/work
  owner: appuser
```

#### `from_archive` — download and extract a direct archive URL

Downloads any archive URL with `curl` and extracts it using IdeGYM's `extract` script.
Use this when you have a pre-packaged tarball or zip that is not a git repository.

```python
Project.from_archive(
    "https://example.com/releases/project-v1.2.0.tar.gz",
    target="/home/appuser/work",
    owner="appuser",
    group="appuser",
)
```

```yaml
- type: project
  source: archive
  url: https://example.com/releases/project-v1.2.0.tar.gz
  target: /home/appuser/work
  owner: appuser
```

#### `from_git_clone` — plain git clone

Runs `git clone <url>` and `git checkout <ref>` directly in the Dockerfile. Simpler than the
download-based approach and works with any git server, but requires `git` to be installed in the image
(the `base-system` plugin installs it by default).

```python
Project.from_git_clone(
    url="https://github.com/your-org/your-repo.git",
    ref="main",  # branch, tag, or commit SHA
    target="/home/appuser/work",
    owner="appuser",
    group="appuser",
)
```

```yaml
- type: project
  source: git-clone
  url: https://github.com/your-org/your-repo.git
  ref: main
  owner: appuser
  target: /home/appuser/work
```

> [!NOTE]
> `from_git_clone` does not support the IdeGYM auth token mechanism. For private repositories,
> embed credentials in the URL or configure SSH before cloning via `run_commands`.

**Notes on `git` and `resource` sources:**
- The download URL and auth token are passed as Docker ARGs (`IDEGYM_PROJECT_ARCHIVE_URL`,
  `IDEGYM_AUTH_TYPE`, `IDEGYM_AUTH_TOKEN`). Kaniko injects them via `--build-arg`.
- `ctx.request` is set to a `DownloadRequest`, which the builder uses to inject these ARGs.
- Only one `git`/`resource` project plugin is allowed per image.

---

### `idegym-server`

Installs the IdeGYM server into the image. This is the standard way to produce an environment image:
start from a plain Debian/Ubuntu base, apply `base-system` and `user`, then apply `idegym-server`
to layer the server runtime on top. After that, add your project plugin and any customizations.

Two sources are supported:

#### `from_local` — build from the local workspace

Use during development or in CI, when the IdeGYM repository is available on the host machine.
The Docker build context is set to the repository root, and all workspace packages are `COPY`ed in.

```python
from idegym.plugins.defaults.image import IdeGYMServer
from from_root import from_root

IdeGYMServer.from_local(root=from_root())  # from_root() returns the repository root
```

```yaml
- type: idegym-server
  source: local
```

#### `from_git` — clone from a remote repository

Use when there is no local workspace available (e.g., in a customer cluster or a standalone build job).
IdeGYM is cloned with `git clone` inside the container at build time. No build context needed.

```python
IdeGYMServer.from_git(
    url="https://github.com/JetBrains-Research/idegym.git",
    ref="main",  # branch, tag, or commit SHA
)
```

```yaml
- type: idegym-server
  source: git
  url: https://github.com/JetBrains-Research/idegym.git
  ref: main
```

> [!NOTE]
> `from_git` requires `git` to be installed in the base image. The `base-system` plugin installs
> it by default. If you start from a plain `debian:*` base without `base-system`, add `git` to your
> package list before this plugin.

**What both sources do:**
- Install `uv` (copied from the official `ghcr.io/astral-sh/uv` image)
- Place IdeGYM workspace packages under `$IDEGYM_PATH` (`/opt/idegym`)
- Set `IDEGYM_PATH`, `IDEGYM_PROJECT_ROOT`, and `PYTHONPATH` environment variables
- Install server Python dependencies via `uv sync` (no dev dependencies)
- Configure `supervisord` for process management
- Expose port 8000 and add a healthcheck

**Typical image structure:**

```python
from idegym.image.builder import Image
from idegym.plugins.defaults.image import BaseSystem, IdeGYMServer, User, Project
from from_root import from_root

# Local build (development / CI)
image = (
    Image.from_base("debian:bookworm-20250520-slim")
    .with_plugin(BaseSystem())
    .with_plugin(User(username="appuser", uid=1000, gid=1000))
    .with_plugin(IdeGYMServer.from_local(root=from_root()))
    .with_plugin(
        Project.from_git(
            url="https://github.com/your-org/your-repo.git",
            ref="abc1234",
            owner="appuser",
        )
    )
)

# Remote build (no local workspace)
image = (
    Image.from_base("debian:bookworm-20250520-slim")
    .with_plugin(BaseSystem())
    .with_plugin(User(username="appuser", uid=1000, gid=1000))
    .with_plugin(
        IdeGYMServer.from_git(
            url="https://github.com/JetBrains-Research/idegym.git",
            ref="main",
        )
    )
    .with_plugin(
        Project.from_git_clone(
            url="https://github.com/your-org/your-repo.git",
            ref="abc1234",
            owner="appuser",
        )
    )
)
```

---

### `pycharm`

Installs PyCharm into the image. Requires PyCharm **2026.1.1 or newer** — older versions are not
supported. Starting with 2026.1.1, there is no community/professional split: the download is unified
and the JetBrains MCP server plugin is bundled.

```python
from idegym.plugins.pycharm.image import PyCharm

PyCharm(
    version="2026.1.1",  # YYYY.N or YYYY.N.N; must be 2026.1.1+
    user="appuser",  # user to switch back to after installation
)
```

```yaml
- type: pycharm
  version: "2026.1.1"
  user: appuser
```

**Notes:**
- Emits `USER root` to install PyCharm and its dependencies, then switches back to `ctx.current_user`
- Version format: `YYYY.N` or `YYYY.N.N` (e.g., `2026.1`, `2026.1.1`)
- See [IdeGYM PyCharm Plugin](https://github.com/JetBrains-Research/idegym/blob/main/plugins/pycharm/README.md) for the full reference

---

## Building Images

### Local Docker build

Build an image using your local Docker daemon:

```python
from idegym.image.builder import Image
from idegym.plugins.defaults.image import BaseSystem, User

image = (
    Image.from_base("ghcr.io/jetbrains-research/idegym/server-debian-bookworm-20250520-slim:latest")
    .named("my-env")
    .with_plugin(User(username="devuser", uid=2000, gid=2000))
    .run_commands("echo 'hello' > /home/devuser/hello.txt")
)

# Build with the local Docker daemon
built = image.build()

# Or with a custom registry prefix
built = image.build(registry="my-registry.example.com/idegym")

print(built.repo_tags)  # ['my-env:latest']
```

The built image tag is based on the `name` field. If `name` is not set, a hash-based tag is generated.

> [!NOTE]
> Plugins whose Dockerfile `COPY`s bundled files (e.g. `idea`/`pycharm` start scripts and the
> open-project zip) declare those files via `get_context_files()`. The build driver stages any missing
> ones in place into the build context directory and cleans them up afterwards, so the build works from
> any working directory — you do **not** need a checkout of the idegym repo. See
> [Plugin Architecture → Shipping files your `COPY` needs](plugins.md#shipping-files-your-copy-needs-get_context_files).

After building, load the image into Minikube for use in pods:

```shell
minikube image load my-env:latest
```

Or reference it directly in a server deployment if the image is in a registry accessible from the cluster.

---

### Kaniko build (in-cluster)

For cluster-based builds, the orchestrator uses [Kaniko](https://github.com/GoogleContainerTools/kaniko)
to build images inside Kubernetes pods. This avoids the need for a Docker daemon on cluster nodes.

**Workflow:**

1. Define the image in Python and serialize it to YAML:
   ```python
   path = image.write_yaml("/tmp/image.yaml")
   ```

2. Submit the YAML to the orchestrator via the client:
   ```python
   from idegym.client.client import IdeGYMClient

   async with IdeGYMClient(...) as client:
       summary = await client.jobs.build_and_push_images(
           path=path,
           namespace="idegym",
           timeout=600,
           poll_interval=10,
       )
       assert summary.failed_jobs == 0
       image_tag = summary.jobs_results[0].tag
   ```

3. The orchestrator creates a Kaniko job that:
   - Uses the `dockerfile_content` from `ImageBuildSpec` (mounted as a ConfigMap)
   - Passes download ARGs as `--build-arg` values (for project plugins)
   - Pushes the result to the configured registry

   For images whose Dockerfile `COPY`s files from the idegym repo (the `idea`/`pycharm` plugins, which
   declare `get_context_files()`), the job's build context is a **git checkout of the idegym repo** —
   `git://github.com/JetBrains-Research/idegym.git#<ref>` — instead of the Dockerfile-only ConfigMap, so
   those `COPY` paths resolve. `<ref>` tracks the orchestrator version (`refs/tags/v<version>`, or
   `refs/heads/main` for `latest`/dev builds), keeping the checkout in sync with the plugin code that
   generated the Dockerfile. Plain download/inline images keep the ConfigMap-only context (no clone).
   Override the source with `IDEGYM_KANIKO_CONTEXT_GIT_URL` / `IDEGYM_KANIKO_CONTEXT_GIT_REF` on the
   orchestrator (e.g. for a fork, a mirror, or to pin an exact commit in CI).

4. Use the returned tag to start a server:
   ```python
   async with client.with_server(image_tag=image_tag, ...) as server:
       result = await server.execute_bash(script="echo hello")
   ```

**Registry considerations:**

The Kaniko registry is configured via environment variables on the orchestrator:

| Variable | Description |
|---|---|
| `DOCKER_REGISTRY` | Registry host/prefix for pushed images |
| `KANIKO_INSECURE_REGISTRY` | `"true"` for HTTP registries (e.g., the Minikube local registry) |

For local development with Minikube, the cluster-internal registry address
`registry.kube-system.svc.cluster.local` is used (requires the `registry` Minikube addon).

**Docker ARGs and `set -u`:**

Kaniko evaluates Docker `ARG` instructions, but ARGs without a corresponding `--build-arg` value are
**unset** (not empty strings). If your Dockerfile uses `set -u` (which IdeGYM's builder does via
`RUN set -eux;`), reference optional ARGs with the `${VAR:-}` syntax:

```dockerfile
ARG IDEGYM_AUTH_TOKEN
RUN set -eux; curl -H "Authorization: ${IDEGYM_AUTH_TOKEN:-}" ...
```

The built-in plugins handle this correctly.

---

### Build backends

The `/api/build-push-images` flow is **backend-agnostic**. The orchestrator selects an
`ImageBuilder` implementation at request time and the shared `ImageBuildService` drives it —
constructing the image tag, persisting status to the `job_statuses` table, and polling until the
build is done. Each backend implements a small interface:

```python
class ImageBuilder:
    async def submit_build(self, tag, spec, *, namespace, service_version) -> BuildHandle: ...
    async def get_status(self, handle) -> Status: ...
```

`submit_build` starts a build; `get_status` reports its progress. The returned `BuildHandle.name`
is the opaque string stored as `JobStatusRecord.job_name` and returned to clients (for Kaniko it is
the Kubernetes Job name; for Cloud Build it is the build id), so the existing
`/api/jobs/status/{job_name}` endpoint works for every backend.

The backend is chosen via `orchestrator.build.backend` (set by `IDEGYM_BUILD_BACKEND`), **defaulting to
`kaniko`** so existing deployments are unchanged:

| Variable | Description | Default |
|---|---|---|
| `IDEGYM_BUILD_BACKEND` | `kaniko` or `cloudbuild_gke` | `kaniko` |

Implementations live in `idegym.backend.utils.image_builder` (`base.py`, `kaniko.py`,
`cloudbuild_gke.py`, `factory.py`). The `BuildBackend` enum is in `idegym.api.image_build`.

---

### GKE Cloud Build backend

`cloudbuild_gke` builds images with [GCP Cloud Build](https://cloud.google.com/build) instead of an
in-cluster Kaniko Job. It uses BuildKit (`DOCKER_BUILDKIT=1`) with a generated `docker build` step —
equivalent to a `cloudbuild.yaml` — so Dockerfile heredocs and `--mount=type=secret` work. The build
context (the rendered `Dockerfile`, mirroring the Kaniko ConfigMap) is uploaded to a GCS staging
bucket; project sources are still fetched at build time via the same archive build args Kaniko uses.
It submits asynchronously and polls the build, via the `google-cloud-build` Python client (no `gcloud`
CLI dependency in the orchestrator image).

**Auth token handling.** Unlike Kaniko (which passes `IDEGYM_AUTH_TOKEN` as a `--build-arg`), the
Cloud Build backend passes the token as a **BuildKit build secret**: `build.steps[].args` are stored on
the Build resource and readable by anyone with build-viewer access, so a `--build-arg` there would leak
the credential. Instead the backend ships the token as a separate file in the (access-controlled) GCS
build context and rewrites the token-consuming `RUN` to `--mount=type=secret,id=idegym_auth_token`,
reading it from `/run/secrets/idegym_auth_token`. The token therefore never appears in the Build
request, its logs, or the image history. This transform is applied only to the Cloud Build context; the
shared rendered Dockerfile — and the Kaniko path, which cannot parse `RUN --mount` — is untouched.

**Configuration** (all under `orchestrator.build.cloudbuild_gke`):

| Variable | Description | Default |
|---|---|---|
| `IDEGYM_CLOUDBUILD_PROJECT_ID` | GCP project that runs the build | _(required)_ |
| `IDEGYM_CLOUDBUILD_REGION` | Cloud Build region (e.g. `europe-west1`) | _(required)_ |
| `IDEGYM_CLOUDBUILD_STAGING_BUCKET` | GCS bucket (name only) for the uploaded context | _(required)_ |
| `IDEGYM_CLOUDBUILD_MACHINE_TYPE` | Worker machine type (e.g. `E2_HIGHCPU_8`) | project default |
| `IDEGYM_CLOUDBUILD_DISK_SIZE_GB` | Worker disk size in GB | project default |
| `IDEGYM_CLOUDBUILD_TIMEOUT_SECONDS` | Per-build timeout | `2400` |
| `IDEGYM_CLOUDBUILD_SKIP_EXISTING` | Skip the build if the image already exists in Artifact Registry | `false` |
| `IDEGYM_CLOUDBUILD_MAX_DISK_SIZE_GB` | Ceiling for a per-request `disk_size_gb` | `1000` |
| `IDEGYM_CLOUDBUILD_ALLOWED_MACHINE_TYPES` | Machine types a request may ask for. Empty rejects any | _(empty)_ |

Two settings apply to **every** backend, under `orchestrator.build`:

| Variable | Description | Default |
|---|---|---|
| `IDEGYM_BUILD_ALLOWED_REGISTRY_PREFIXES` | Registry prefixes a request may push to when it supplies its own `tag`/`registry`. Empty refuses caller-supplied destinations altogether | _(empty)_ |
| `IDEGYM_BUILD_MAX_TIMEOUT_SECONDS` | Ceiling for a per-request `timeout_seconds` | `7200` |

`project_id`, `region`, and `staging_bucket` are required when this backend is selected (validated at
config load). `DOCKER_REGISTRY` should point at an Artifact Registry repository
(`<region>-docker.pkg.dev/<project>/<repo>`).

**Required GCP IAM / credentials.** Auth relies on the orchestrator pod's ambient credentials
(service account via Workload Identity). The service account needs:

- **Cloud Build Editor** (`roles/cloudbuild.builds.editor`) — submit and read builds.
- **Artifact Registry Writer** (`roles/artifactregistry.writer`) — push images (and read, for
  `skip_existing`).
- **Storage Object Admin** (`roles/storage.objectAdmin`) on the staging bucket — upload the build
  context.

```shell
IDEGYM_BUILD_BACKEND=cloudbuild_gke
IDEGYM_CLOUDBUILD_PROJECT_ID=my-gcp-project
IDEGYM_CLOUDBUILD_REGION=europe-west1
IDEGYM_CLOUDBUILD_STAGING_BUCKET=my-idegym-build-context
DOCKER_REGISTRY=europe-west1-docker.pkg.dev/my-gcp-project/idegym
```

**Smoke test.** The Kaniko backend is covered by the kind-based e2e suite, but Cloud Build needs a real
GCP project and so cannot run there. `scripts/cloudbuild_gke_smoke_test.py` exercises the backend
end-to-end against live GCP: it renders each image in a YAML file exactly as the orchestrator does,
submits a Cloud Build per image via the same `build_image_builder` factory, polls until each finishes,
and then confirms the pushed image resolves in Artifact Registry. Authenticate with
`gcloud auth application-default login` (as a principal holding the IAM roles above), then:

```shell
uv run python scripts/cloudbuild_gke_smoke_test.py \
    --images scripts/cloudbuild_gke_smoke_images.example.yaml \
    --project-id my-gcp-project --region europe-west1 \
    --staging-bucket my-idegym-build-context \
    --registry europe-west1-docker.pkg.dev/my-gcp-project/idegym
```

The example YAML builds two images without a project download; add a `project` plugin with a URL and
token to also exercise the auth-token BuildKit secret mount. The script exits non-zero if any build
fails or its image is not found afterwards.

---

### Backend capability matrix

Everything above works on both backends except where the underlying builder makes it impossible.
Those cases are validated **before** a build is submitted and reported as an error, not discovered
from a build log.

| Capability | `kaniko` | `cloudbuild_gke` |
|---|---|---|
| Inline `base_dockerfile` | yes | yes |
| `context_uri` | `gs://`, `s3://`, `https://`, `git://` | `gs://` only |
| `context_uri` **and** plugin `context_files` together | **no** — see below | yes |
| Plugin `context_files` (idea/pycharm scripts) | yes, via a git checkout | yes, packed into the context tar |
| BuildKit-only syntax (heredocs, `RUN --mount`, `COPY --link`) | **no** — rejected | yes, via `BUILDKIT_SYNTAX` |
| `secret_build_args` (from the orchestrator's environment) | yes | yes |
| `secrets` (Secret Manager) | as build args — **exposed**, see below | as BuildKit secret mounts |
| `machine_type` / `disk_size_gb` | no — use `resources` | yes |
| `timeout_seconds` | yes (monitor deadline only) | yes |
| `skip_existing` | no — an identical resubmission rebuilds | yes |

**`context_uri` and `context_files` cannot combine on Kaniko.** Kaniko accepts a single `--context`.
Plugin context files are resolved by pointing that context at a git checkout of the idegym repo, so a
caller-supplied context takes the same slot. An image that needs both — for instance a caller context
*plus* the `idea` plugin — must use `cloudbuild_gke`, which overlays them. The error names both
sources.

**`skip_existing` is Cloud Build only.** On Kaniko, resubmitting an identical definition rebuilds
rather than short-circuiting. Callers that care about avoiding redundant builds should check the
registry themselves before submitting.

### Build secrets across backends

There are two separate mechanisms, and they need **different Dockerfiles**:

| | `secret_build_args` | `secrets` |
|---|---|---|
| Value comes from | the orchestrator's own environment | Secret Manager, per request |
| Declared by | a plugin's `get_build_secrets()` | the caller |
| Read in the Dockerfile as | `$NAME` | `$NAME` on Kaniko, a **mount** on Cloud Build |

On `cloudbuild_gke`, a `secrets` entry becomes a real BuildKit secret mount and is read from the
filesystem:

```dockerfile
RUN --mount=type=secret,id=gh_token \
    git clone https://x-access-token:"$(cat /run/secrets/gh_token)"@github.com/org/private.git
```

:::danger `secrets` under Kaniko is exposed in the image
Kaniko has no `--mount=type=secret`. A `secrets` entry there is resolved and passed as a
`--build-arg`, which means:

- the value is **recorded in the image layer history** — `docker history` on the pushed image reveals
  it;
- the value is readable by anyone with `get jobs` / `get pods` in the build namespace.

The build emits a warning naming the backend and the affected ids, and that warning is persisted on
the job record so it is visible after the fact. Treat images built this way as sensitive, prefer
short-lived credentials, and keep the build namespace's RBAC tight.
:::

Because the two mechanisms read the secret differently, **supplying `secrets` on Kaniko does not make
a mount-style Dockerfile work there** — `RUN --mount` is still rejected. A Dockerfile that consumes
secrets via mounts is `cloudbuild_gke`-only, whatever else you configure.

**Additional IAM.** Beyond the roles listed above, the builder's service account needs
`roles/secretmanager.secretAccessor` on every secret named in `secrets`, and read access on the bucket
a `context_uri` points at. A cross-project context needs an explicit grant. Scope both deliberately
rather than broadly: a caller-supplied `context_uri` means the build reads from a bucket the caller
controls.

### Build cost and deduplication

Deduplication is a property of **tag identity**, not of who assembles the Dockerfile. Two byte-identical
definitions produce an identical merged Dockerfile, therefore an identical `image_version()`, therefore
the same tag — so the second submission is a tag hit rather than a rebuild. Counting builds for two
tasks:

| | identical environments | different environments |
|---|---|---|
| Published base, referenced by tag | 1 base + 1 derived | 2 base + 2 derived |
| Inline base | **1 total** (second is a tag hit) | 2 total |

An inline base is therefore equal or better on build count, and additionally saves a build submission
and an intermediate push per image.

Two things remain true, neither introduced by the inline-base flow:

- **No layer reuse between different-but-similar images.** Fifty environments sharing a 2 GB toolchain
  layer but differing in one commit rebuild that layer fifty times. This is equally true of the
  published-base flow, since each of those is a distinct base image. Layer caching
  (`--cache=true --cache-repo=…` for Kaniko; `--cache-from` with `BUILDKIT_INLINE_CACHE=1` for Cloud
  Build) would fix it for both, and is not implemented.
- **Kaniko snapshots every stage**, so build-pod memory and disk grow with an inline base. The
  per-image `resources` field is the lever; on Cloud Build it is `machine_type` / `disk_size_gb`.

The tag is a pure function of `(base_dockerfile, base_stage, context_uri, plugins, commands,
secret names)`. Build resources and the destination registry deliberately do **not**
participate — they do not change image content. Supplying `version` opts out of hash-based dedupe
entirely, which is the point when a caller maintains its own content-addressed tags.

---

## Writing Custom Plugins

A plugin is a Pydantic model that inherits from `PluginBase` and is registered with `@image_plugin`.

```python
from idegym.api.plugin import BuildContext, PluginBase, image_plugin


@image_plugin("my-plugin")
class MyPlugin(PluginBase):
    message: str
    path: str = "/tmp/hello.txt"

    def apply(self, ctx: BuildContext) -> BuildContext:
        # Optionally update the build context.
        # For example, set a custom user or add labels:
        return ctx.updated(labels={**ctx.labels, "my.plugin": "true"})

    def render(self, ctx: BuildContext) -> str:
        # Return a Dockerfile fragment (no leading/trailing newlines needed).
        return f"RUN echo {self.message!r} > {self.path}"
```

Once registered, the plugin can be used in Python:

```python
image = Image.from_base(...).with_plugin(MyPlugin(message="hello", path="/tmp/hello.txt"))
```

And in YAML (the `type` field matches the name passed to `@image_plugin`):

```yaml
plugins:
  - type: my-plugin
    message: hello
    path: /tmp/hello.txt
```

**`BuildContext` reference:**

| Field | Type | Default | Description |
|---|---|---|---|
| `base` | `str` | — | Base image reference |
| `current_user` | `str` | `"root"` | Current user (updated by `User` plugin) |
| `home` | `str` | `"/root"` | Current user's home directory |
| `project_root` | `str` | `"/root/work"` | Project root path inside the container |
| `request` | `Optional[DownloadRequest]` | `None` | Download request (set by `Project` plugin) |
| `labels` | `dict[str, str]` | `{}` | Docker image labels |
| `context_path` | `str` | `"."` | Docker build context path |
| `extras` | `dict[str, Any]` | `{}` | Plugin-defined arbitrary state |

Use `ctx.updated(**kwargs)` to return a modified copy. Use `ctx.with_extra("key", value)` to
pass data between plugins via `extras`.

**Important:**
- Plugins are discovered automatically via the `idegym.plugins.image` entry point group. Declare
  your plugin in `[project.entry-points."idegym.plugins.image"]` in `pyproject.toml` and it will
  be available for YAML deserialization as soon as your package is installed.
- Plugin `type` names must be unique across the registry.

---

> **See also:** [Plugin Architecture](plugins.md) — full guide covering server plugins, client
> operation plugins, MCP upstream convention, the `plugins.json` configuration file, and how to
> write a plugin that participates in all integration points.
