# Full Flow Example

This walkthrough demonstrates the complete IdeGYM workflow end-to-end:

1. Define a custom environment image using the Python API
2. Build and push it via Kaniko (in-cluster)
3. Start an IdeGYM server from the built image
4. Execute commands inside the running environment
5. Tear down the server

Two scenarios are covered:

- **[Scenario A](#scenario-a-pre-built-idegym-base-image)** — build on top of a pre-built IdeGYM base image (fast, recommended for most cases)
- **[Scenario B](#scenario-b-build-idegym-from-source)** — start from plain Debian and install IdeGYM from source using `IdeGYMServer.from_git` (useful when you want to pin to a specific IdeGYM commit or don't have access to GHCR)

> For local Docker builds and loading images into Minikube without a registry, see [Local Deployment](local_deployment.md).

## Prerequisites

- A running cluster with IdeGYM deployed (see [Local Deployment](local_deployment.md) or [Remote Deployment](remote_deployment.md))
- Project dependencies installed: `uv sync --all-packages --all-extras --all-groups`
- The orchestrator reachable at the configured URL

---

## Scenario A: Pre-built IdeGYM base image

Start from an image that already has the IdeGYM server installed, and layer your user,
project, and any extra commands on top. This is the fastest path since the server
installation is already baked into the base.

### 1. Define the image

```python
from idegym.image.builder import Image
from idegym.image.plugins import Permissions, Project, User

BASE_IMAGE = "ghcr.io/jetbrains-research/idegym/server-debian-bookworm-20250520-slim:latest"
# When deploying to Minikube via e2e tests, use the cluster-local registry:
# BASE_IMAGE = "registry.kube-system.svc.cluster.local/server-debian-bookworm-20250520-slim:latest"

image = (
    Image.from_base(BASE_IMAGE)
    .with_plugin(User(username="devuser", uid=2000, gid=2000, sudo=True))
    .with_plugin(
        Permissions(paths={"/home/devuser": {"owner": "devuser", "mode": "755"}})
    )
    .with_plugin(
        Project.from_git(
            url="https://github.com/owner/my-repo.git",
            ref="abc123def456",
            owner="devuser",
            target="/home/devuser/project",
        )
    )
    .run_commands("echo 'environment ready' > /home/devuser/ready.txt")
    .with_runtime(
        runtime_class_name="gvisor",
        resources={
            "requests": {"cpu": "500m", "memory": "512Mi", "ephemeral-storage": "1Gi"},
            "limits":   {"cpu": "500m", "memory": "512Mi", "ephemeral-storage": "1Gi"},
        },
    )
)
```

### 2. Build, start, and run

```python
import asyncio
from pathlib import Path
import tempfile

from idegym.client.client import IdeGYMClient
from idegym.api.auth import BasicAuth

async def main():
    async with IdeGYMClient(
        orchestrator_url="http://idegym-local.test",
        name="my-client",
        namespace="idegym-local",
        auth=BasicAuth(username="test", password="test"),
    ) as client:

        # Submit a Kaniko build job and wait for it to finish.
        with tempfile.TemporaryDirectory() as tmp:
            yaml_path = image.write_yaml(Path(tmp) / "image.yaml")
            summary = await client.jobs.build_and_push_images(
                path=yaml_path,
                namespace="idegym-local",
                timeout=600,
                poll_interval=10,
            )

        if summary.failed_jobs > 0:
            raise RuntimeError(f"Build failed: {summary.jobs_results[0].details}")

        image_tag = summary.jobs_results[0].tag
        print(f"Built image: {image_tag}")

        # Start the server and run commands inside it.
        async with client.with_server(
            image_tag=image_tag,
            server_name="my-dev-server",
            runtime_class_name="gvisor",
            run_as_root=True,
            resources=...,   # kubernetes.client.V1ResourceRequirements
            server_start_wait_timeout_in_seconds=600,
        ) as server:

            result = await server.execute_bash(script="id devuser", command_timeout=30.0)
            print(f"User: {result.stdout.strip()}")

            result = await server.execute_bash(
                script="ls /home/devuser/project/", command_timeout=30.0
            )
            print(f"Project files: {result.stdout.strip()}")

        # Server is stopped automatically when the `with_server` block exits.
        print("Done.")

asyncio.run(main())
```

---

## Scenario B: Build IdeGYM from source

Use this when you need to pin to a specific IdeGYM commit, or when the pre-built GHCR image
is not available. `IdeGYMServer.from_git` clones the IdeGYM repository inside the container
at build time and installs the server from source — no pre-built base image required.

Requires `git` in the base image. `BaseSystem()` (the default package set) already includes it.

### 1. Define the image

```python
from idegym.image.builder import Image
from idegym.image.plugins import BaseSystem, IdeGYMServer, Project, User

IDEGYM_REPO = "https://github.com/jetbrains-research/idegym-oss.git"
IDEGYM_REF  = "main"   # pin to a tag or commit SHA for reproducibility

image = (
    Image.from_base("debian:bookworm-slim")
    .with_plugin(BaseSystem())           # installs git, curl, dumb-init, etc.
    .with_plugin(User(username="devuser", uid=2000, gid=2000, sudo=True))
    .with_plugin(IdeGYMServer.from_git(url=IDEGYM_REPO, ref=IDEGYM_REF))
    .with_plugin(
        Project.from_git(
            url="https://github.com/owner/my-repo.git",
            ref="abc123def456",
            owner="devuser",
            target="/home/devuser/project",
        )
    )
    .run_commands("echo 'environment ready' > /home/devuser/ready.txt")
    .with_runtime(
        runtime_class_name="gvisor",
        resources={
            "requests": {"cpu": "500m", "memory": "1Gi", "ephemeral-storage": "2Gi"},
            "limits":   {"cpu": "500m", "memory": "1Gi", "ephemeral-storage": "2Gi"},
        },
    )
)
```

> **Note:** Building from source takes longer than using a pre-built base image because
> `uv python install` and `uv sync` run inside the Kaniko job. Allocate extra `timeout`
> in the build call if needed.

### 2. Build, start, and run

The build and server usage is identical to Scenario A — just substitute this image definition:

```python
import asyncio
from pathlib import Path
import tempfile

from idegym.client.client import IdeGYMClient
from idegym.api.auth import BasicAuth

async def main():
    async with IdeGYMClient(
        orchestrator_url="http://idegym-local.test",
        name="my-client",
        namespace="idegym-local",
        auth=BasicAuth(username="test", password="test"),
    ) as client:

        with tempfile.TemporaryDirectory() as tmp:
            yaml_path = image.write_yaml(Path(tmp) / "image.yaml")
            summary = await client.jobs.build_and_push_images(
                path=yaml_path,
                namespace="idegym-local",
                timeout=1200,    # allow extra time for source build
                poll_interval=15,
            )

        if summary.failed_jobs > 0:
            raise RuntimeError(f"Build failed: {summary.jobs_results[0].details}")

        image_tag = summary.jobs_results[0].tag
        print(f"Built image: {image_tag}")

        async with client.with_server(
            image_tag=image_tag,
            server_name="my-from-source-server",
            runtime_class_name="gvisor",
            run_as_root=True,
            resources=...,
            server_start_wait_timeout_in_seconds=600,
        ) as server:

            result = await server.execute_bash(
                script="cat /home/devuser/ready.txt", command_timeout=30.0
            )
            print(f"Ready: {result.stdout.strip()}")

asyncio.run(main())
```

---

## Inspecting the generated Dockerfile

Before submitting a build, you can inspect the Dockerfile that the image definition
will produce:

```python
spec = image.to_spec()
print(spec.dockerfile_content)
```

For Scenario A this produces something like:

```dockerfile
FROM ghcr.io/jetbrains-research/idegym/server-debian-bookworm-20250520-slim:latest

SHELL ["/bin/bash", "-c"]

USER root

ARG IDEGYM_PROJECT_ARCHIVE_URL
ARG IDEGYM_PROJECT_ARCHIVE_PATH
ARG IDEGYM_AUTH_TOKEN
ARG IDEGYM_AUTH_TYPE

ENV IDEGYM_PROJECT_ARCHIVE_URL="$IDEGYM_PROJECT_ARCHIVE_URL"
ENV IDEGYM_PROJECT_ARCHIVE_PATH="$IDEGYM_PROJECT_ARCHIVE_PATH"

ENV IDEGYM_PROJECT_ROOT="/home/devuser/project"

# Create user devuser (uid=2000)
RUN set -eux; \
    groupadd --gid 2000 devuser; \
    useradd --uid 2000 --gid 2000 --shell /bin/bash --create-home devuser; \
    echo 'devuser ALL=(ALL) NOPASSWD:ALL' >> /etc/sudoers

# Set permissions
RUN set -eux; \
    chown devuser:devuser /home/devuser && chmod 755 /home/devuser

# Download project
RUN set -eux; \
    ...

USER devuser

RUN set -eux; \
    echo 'environment ready' > /home/devuser/ready.txt
```

For Scenario B (`IdeGYMServer.from_git`) the generated Dockerfile is much longer — it
includes the `git clone`, file-copy setup, `uv python install`, `uv sync`, and the
`ENTRYPOINT`/`HEALTHCHECK` directives that the base image already provides in Scenario A.

---

## What the orchestrator does internally

When `client.jobs.build_and_push_images()` is called:

1. The client uploads the YAML to `POST /api/jobs/build-images`
2. The orchestrator parses the YAML into `Image` objects and calls `image.to_spec()` on each
3. For each image the orchestrator creates a Kubernetes Job running a Kaniko container:
   - `dockerfile_content` is passed as the Dockerfile
   - Download ARGs (project URL, auth token) are passed as `--build-arg` values
   - `--destination` points to the cluster-internal registry
4. The orchestrator polls the jobs and returns a summary with the pushed image tags
5. The client returns the summary to the caller

---

## Next Steps

- [Image Builder](image_builder.md) — full plugin reference and YAML format
- [Client Library](client.md) — complete API for starting servers and running tasks
- [Local Deployment](local_deployment.md) — set up Minikube, including local Docker builds
- [Remote Deployment](remote_deployment.md) — deploy to a production cluster
- [E2E Tests](../e2e-tests/README.md) — runnable end-to-end examples
