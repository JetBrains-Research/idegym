#!/usr/bin/env bash

# TEMPORARY WORKAROUND. Remove this script, and every call to it, once
# https://github.com/kubernetes/minikube/issues/23709 is closed and a fixed minikube is released.
#
# minikube's `gvisor` addon downloads gVisor's `latest` release every time it is enabled. Since
# gVisor 20260831.0, `runsc` and `containerd-shim-runsc-v1` are no longer published as individual
# files, so the addon saves a 404 error page as the shim, and every `runtimeClassName: gvisor` pod
# fails with `FailedCreatePodSandBox ... failed to start shim`. This affects every minikube
# version, so pinning minikube does not help.
#
# The script overwrites both binaries on the minikube node with the last release that still ships
# them individually. Run it after `minikube start --addons=gvisor,...`, and again after every
# restart: the addon re-downloads the broken files each time it is enabled. No containerd restart
# is needed — the addon has already configured the runtime, and both binaries are exec'd per pod.

set -euo pipefail

GVISOR_RELEASE="20260817.0"

arch="$(minikube ssh -- uname -m | tr -d '\r')"
url="https://storage.googleapis.com/gvisor/releases/release/${GVISOR_RELEASE}/${arch}"
workdir="$(mktemp -d)"
trap 'rm -rf "$workdir"' EXIT

for binary in runsc containerd-shim-runsc-v1; do
  echo "* Installing gVisor ${GVISOR_RELEASE} \`${binary}\` (${arch}) on the minikube node..."
  curl -fsSL -o "${workdir}/${binary}" "${url}/${binary}"
  curl -fsSL -o "${workdir}/${binary}.sha512" "${url}/${binary}.sha512"
  (cd "$workdir" && shasum -a 512 -c "${binary}.sha512")
  minikube cp "${workdir}/${binary}" "/tmp/${binary}"
  minikube ssh -- sudo install -m 0755 "/tmp/${binary}" "/usr/bin/${binary}"
done

minikube ssh -- /usr/bin/runsc --version
