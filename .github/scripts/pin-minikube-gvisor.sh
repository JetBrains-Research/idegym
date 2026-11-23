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

binaries=(runsc containerd-shim-runsc-v1)

for binary in "${binaries[@]}"; do
  echo "* Downloading gVisor ${GVISOR_RELEASE} \`${binary}\` (${arch})..."
  curl -fsSL -o "${workdir}/${binary}" "${url}/${binary}"
  curl -fsSL -o "${workdir}/${binary}.sha512" "${url}/${binary}.sha512"
  (cd "$workdir" && shasum -a 512 -c "${binary}.sha512")
done

install_binaries() {
  for binary in "${binaries[@]}"; do
    # Not `/tmp/${binary}`: the addon creates `/tmp/runsc` on the node as its log directory.
    staged="/tmp/idegym-gvisor-${binary}"
    minikube cp "${workdir}/${binary}" "$staged"
    minikube ssh -- sudo install -m 0755 "$staged" "/usr/bin/${binary}"
    minikube ssh -- sudo rm -f "$staged"
  done
}

binaries_are_pinned() {
  for binary in "${binaries[@]}"; do
    minikube ssh -- "sudo sha512sum /usr/bin/${binary}" | grep -q "$(cut -d' ' -f1 "${workdir}/${binary}.sha512")" || return 1
  done
}

# The addon downloads asynchronously from its own pod, so it can finish after an install here and
# put the broken files back. Re-install until both binaries survive a settle period unchanged.
for attempt in 1 2 3 4 5 6; do
  echo "* Installing pinned gVisor binaries on the minikube node (attempt ${attempt})..."
  install_binaries
  sleep 20
  if binaries_are_pinned; then
    minikube ssh -- /usr/bin/runsc --version
    exit 0
  fi
  echo "* The gvisor addon overwrote the pinned binaries; installing them again."
done

echo "gVisor binaries on the minikube node keep being overwritten; giving up." >&2
exit 1
