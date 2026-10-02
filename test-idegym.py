#!/usr/bin/env -S uv run python
"""IdeGYM walkthrough: start a server, snapshot it, and restore a second server from the snapshot.

The script reads top to bottom as one scenario:

1. Register a client with the orchestrator.
2. Start server #1 from the pre-built server image, run a command, and write a file.
3. Snapshot server #1, then stop it.
4. Start server #2 from that snapshot and check the file is already there.
5. Stop server #2.

Pod snapshots are GKE-only: the orchestrator needs ``podSnapshot.enabled`` and the server must
run under gVisor on a non-E2 node (see ``website/docs/reference/remote_deployment.md``).

Example:
    uv run python scripts/hello_idegym.py
"""

import asyncio

from idegym.api.auth import BasicAuth
from idegym.api.cpu import CpuQuantity
from idegym.api.memory import MemoryQuantity
from idegym.api.orchestrator.servers import ErrorResponse, ServerReuseStrategy, SnapshotRef
from idegym.api.resources import KubernetesResources, ResourceQuantities
from idegym.client.client import IdeGYMClient, ServerCloseAction
from idegym.client.operations.utils import PollingConfig

IMAGE = "ghcr.io/jetbrains-research/idegym/server-debian-bookworm-20250520-slim:0.12.0"
NAMESPACE = "idegym"
FILE_PATH = "/tmp/hello.txt"
FILE_CONTENT = "Hello from IdeGYM!\n"

# Node selector that keeps both servers off E2 nodes. GKE pod snapshots (default ``whole-pod``
# scope) do not support E2 machine types, and a restore must land on the same machine series and
# CPU architecture as the snapshot. Pinning the machine family on both servers covers both rules.
# Any non-E2 CPU family the cluster can provision works ("n2", "n2d", "c3", ...); GPU snapshots
# are limited to g2-standard-*, a2-highgpu-1g, a2-ultragpu-1g, and a3-highgpu-1g, and TPUs are
# not supported at all. See https://docs.cloud.google.com/kubernetes-engine/docs/concepts/pod-snapshots
NODE_SELECTOR = {"cloud.google.com/machine-family": "n2"}


async def main() -> None:
    async with IdeGYMClient(
        orchestrator_url="https://idegym-workshop.dev-dws-jbr-europe-west4-gke.intellij.net/",
        name="hello-idegym-client",
        namespace=NAMESPACE,
        #1Password
        auth=BasicAuth(username="", password=""),
    ) as client:
        # ---- Server #1: do some work, then snapshot it ------------------------------------------
        # STOP (not the default FINISH) deletes the server on exit. Otherwise the second
        # with_server below could be handed this same server back through reuse, and the
        # snapshot would never be restored.
        async with client.with_server(
            image_tag=IMAGE,
            server_name="hello-idegym",
            namespace=NAMESPACE,
            runtime_class_name="gvisor",
            run_as_root=True,
            resources=KubernetesResources(
                requests=ResourceQuantities(cpu=CpuQuantity(millicores=250), memory=MemoryQuantity(mi=512)),
                limits=ResourceQuantities(cpu=CpuQuantity(millicores=500), memory=MemoryQuantity(gi=1)),
            ),
            node_selector=NODE_SELECTOR,
            server_start_wait_timeout_in_seconds=300,
            close_action=ServerCloseAction.STOP,
        ) as server:
            print(f"[server #1] started (id={server.server_id})")

            result = await server.execute_bash(script="uname -a && whoami && pwd", command_timeout=30.0)
            print(f"[server #1] bash exit_code={result.exit_code}\n{result.stdout.strip()}")
            assert result.exit_code == 0, result.stderr

            await server.create_file(file_path=FILE_PATH, content=FILE_CONTENT)
            print(f"[server #1] wrote {FILE_PATH}")

            # The 0.12.0 server trims bash output, so print a marker after the file to keep its
            # trailing newline, then cut the marker off again.
            result = await server.execute_bash(script=f"cat {FILE_PATH} && printf '%s' __EOF__", command_timeout=30.0)
            content = result.stdout.removesuffix("__EOF__")
            print(f"[server #1] read back {content!r}")
            assert content == FILE_CONTENT

            # The orchestrator waits up to two minutes for GKE to finish the checkpoint, longer than
            # the client's default 60-second polling budget, so poll for longer here.
            snapshot = await server.snapshot(
                polling_config=PollingConfig(wait_timeout_in_sec=300, poll_interval_in_sec=2)
            )
            # A failed snapshot comes back as an ErrorResponse instead of raising.
            assert not isinstance(snapshot, ErrorResponse), f"snapshot failed: {snapshot}"
            print(f"[server #1] snapshot taken: id={snapshot.snapshot_id} tag={snapshot.snapshot_tag}")

        print("[server #1] stopped")

        # ---- Server #2: restore from the snapshot -----------------------------------------------
        # A restore only works when the configuration matches the snapshotted server exactly:
        # same image, name, runtime, root flag, resources, and machine family. NONE skips reuse so
        # a fresh pod is always created from the snapshot.
        async with client.with_server(
            image_tag=IMAGE,
            server_name="hello-idegym",
            namespace=NAMESPACE,
            runtime_class_name="gvisor",
            run_as_root=True,
            resources=KubernetesResources(
                requests=ResourceQuantities(cpu=CpuQuantity(millicores=250), memory=MemoryQuantity(mi=512)),
                limits=ResourceQuantities(cpu=CpuQuantity(millicores=500), memory=MemoryQuantity(gi=1)),
            ),
            node_selector=NODE_SELECTOR,
            server_start_wait_timeout_in_seconds=300,
            snapshot=SnapshotRef(id=snapshot.snapshot_id, tag=snapshot.snapshot_tag),
            reuse_strategy=ServerReuseStrategy.NONE,
            close_action=ServerCloseAction.STOP,
        ) as server:
            print(f"[server #2] started from snapshot (id={server.server_id})")

            # Nothing wrote this file in server #2; it is there because the pod came back from the
            # checkpoint of server #1 rather than from the image.
            result = await server.execute_bash(script=f"cat {FILE_PATH} && printf '%s' __EOF__", command_timeout=30.0)
            content = result.stdout.removesuffix("__EOF__")
            print(f"[server #2] read back {content!r}")
            assert content == FILE_CONTENT, "server #2 does not carry the snapshotted state"

        print("[server #2] stopped")

    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())