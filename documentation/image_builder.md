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
- [Writing Custom Plugins](#writing-custom-plugins)

---

## Architecture

An `Image` is an immutable description of a Docker image. It consists of:

- A **base image** (e.g., a pre-built IdeGYM server image or a plain Debian image)
- An ordered list of **plugins** — each plugin modifies a shared `BuildContext` and emits a Dockerfile fragment
- Optional **shell commands** appended after all plugin fragments
- Runtime configuration (Kubernetes runtime class, resource requests/limits)

When you call `image.to_spec()` (or `image.build()`), the builder:

1. Creates a `BuildContext` with defaults (`current_user="root"`, `home="/root"`, `project_root="/root/work"`)
2. Iterates through plugins in order; each plugin:
   - `apply(ctx)` — updates the context (e.g., sets `current_user` after creating a user)
   - `render(ctx)` — returns a Dockerfile fragment string
3. Assembles the final Dockerfile: `FROM` clause, optional ARGs for downloads, `ENV` declarations,
   all plugin fragments, the final `USER` line, and the commands block

```
Image.to_spec()
  ├─ BuildContext(base=..., current_user="root", home="/root", ...)
  ├─ plugin[0].apply(ctx) → new ctx
  ├─ plugin[0].render(ctx) → Dockerfile fragment
  ├─ plugin[1].apply(ctx) → new ctx
  ├─ plugin[1].render(ctx) → Dockerfile fragment
  └─ assemble Dockerfile → ImageBuildSpec
```

The resulting `ImageBuildSpec` contains the complete `dockerfile_content` string and any associated
metadata (download request, labels, platforms, runtime config).

---

## Python Fluent API

The `Image` class provides a chainable API. Every method returns a new `Image` (the class is immutable).

### Import

```python
from idegym.image.builder import Image
from idegym.image.plugins import BaseSystem, User, Permissions, Project, IdeGYMServer, PyCharm
```

### `Image.from_base(base)`

Create an image from a base image reference:

```python
image = Image.from_base("ghcr.io/jetbrains-research/idegym/server-debian-bookworm-20250520-slim:latest")
```

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
        "limits":   {"cpu": "1",    "memory": "1Gi",   "ephemeral-storage": "2Gi"},
    },
)
```

### Chaining example

```python
from idegym.image.builder import Image
from idegym.image.plugins import BaseSystem, User, Project, Permissions

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
    .with_plugin(
        Permissions(paths={"/home/devuser/project": {"owner": "devuser", "mode": "755"}})
    )
    .pip_install("pytest", "black")
    .with_runtime(
        runtime_class_name="gvisor",
        resources={
            "requests": {"cpu": "500m", "memory": "512Mi"},
            "limits":   {"cpu": "1",    "memory": "1Gi"},
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
  - base: <base-image>        # Required: full Docker image reference
    name: <output-tag>        # Optional: tag for the built image
    plugins:                  # Optional: list of plugins
      - type: <plugin-type>
        <plugin-fields>: ...
    commands:                 # Optional: shell commands (no RUN prefix)
      - echo "hello"
    runtime_class_name: gvisor  # Default: gvisor
    resources:                  # Optional: Kubernetes resource spec
      requests:
        cpu: "500m"
        memory: "512Mi"
      limits:
        cpu: "1"
        memory: "1Gi"
```

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
from idegym.image.plugins import BaseSystem

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
from idegym.image.plugins import User

User(
    username="appuser",
    uid=1000,
    gid=1000,
    group="appuser",              # Optional primary group name (defaults to username)
    home="/home/appuser",         # Optional, defaults to /home/<username>
    shell="/bin/bash",            # Optional, defaults to /bin/bash
    sudo=True,                    # Grant passwordless sudo
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
from idegym.image.plugins import Permissions

Permissions(
    paths={
        "/home/appuser":         {"owner": "appuser", "mode": "755"},
        "/home/appuser/.config": {"owner": "appuser"},          # mode optional
        "/var/log/app":          {"mode": "777"},                # owner optional
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
from idegym.image.plugins import Project

Project.from_git(
    url="https://github.com/your-org/your-repo.git",
    ref="abc1234",                 # branch, tag, or commit SHA (pin to a SHA for reproducibility)
    owner="appuser",               # file ownership inside the container
    target="/home/appuser/work",   # destination path (defaults to $HOME/work)
    group="appuser",               # optional group (defaults to owner)
    auth=None,                     # optional Authorization for private repos
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
    path="scripts/setup.sh",       # path to the file inside the repo
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
    path="./my-project",           # path relative to the build context
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
    ref="main",                    # branch, tag, or commit SHA
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
from idegym.image.plugins import IdeGYMServer
from from_root import from_root

IdeGYMServer.from_local(root=from_root())   # from_root() returns the repository root
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
    ref="main",    # branch, tag, or commit SHA
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
from idegym.image.plugins import BaseSystem, IdeGYMServer, User, Project
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
    .with_plugin(IdeGYMServer.from_git(
        url="https://github.com/JetBrains-Research/idegym.git",
        ref="main",
    ))
    .with_plugin(
        Project.from_git_clone(
            url="https://github.com/your-org/your-repo.git",
            ref="abc1234",
            owner="appuser",
        )
    )
)

---

### `pycharm`

Installs PyCharm IDE and Java (via SDKMAN) into the image.

```python
from idegym.image.plugins import PyCharm

PyCharm(
    version="2024.3.1",            # YYYY.N or YYYY.N.N
    edition="professional",        # "professional" or "community"
    user="appuser",                # user who will run PyCharm
)
```

```yaml
- type: pycharm
  version: "2024.3.1"
  edition: professional
  user: appuser
```

**Notes:**
- Emits `USER root` to install Java and PyCharm, then switches back to the user from `ctx.current_user`
- Requires a `User` plugin to have run first to set `ctx.current_user`
- Version format: `YYYY.N` or `YYYY.N.N` (e.g., `2024.3`, `2024.3.1`)

---

## Building Images

### Local Docker build

Build an image using your local Docker daemon:

```python
from idegym.image.builder import Image
from idegym.image.plugins import BaseSystem, User

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
   - Uses the `dockerfile_content` from `ImageBuildSpec`
   - Passes download ARGs as `--build-arg` values (for project plugins)
   - Pushes the result to the configured registry

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
        return f'RUN echo {self.message!r} > {self.path}'
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
