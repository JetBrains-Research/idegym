# Local Deployment Guide for MacOS

This document contains step-by-step instructions for running the IdeGYM orchestrator locally on macOS.
It assumes you already have the [brew](https://brew.sh) package manager installed.

## Prerequisites

We will first go over all the software one needs to install and pre-configure to work with a cluster.

### Install Docker

```shell
brew install --cask docker-desktop
```

### Install Kubernetes tools

First, install the requisite software ([kubectl](https://kubernetes.io/docs/reference/kubectl/kubectl) and
[minikube](https://minikube.sigs.k8s.io)):

```shell
brew install kubernetes-cli minikube
```

Then, start the cluster with:

```shell
minikube start \
  --addons=gvisor,ingress \
  --container-runtime=containerd \
  --docker-opt containerd=/var/run/containerd/containerd.sock \
  --namespace=idegym \
  --kubernetes-version=v1.35.0
```

This will:

- Create a new Kubernetes cluster with the specified version;
- Set the default namespace to `idegym`.
- Install the `gvisor` and `ingress` addons;
- Set up the `containerd` container runtime;

All that's left is to create the aforementioned namespace:

```shell
kubectl create namespace idegym
```

> [!NOTE]
> While not mandatory, we also recommend downloading, enabling and using the
> [Kubernetes plugin for IDEA](https://plugins.jetbrains.com/plugin/10485-kubernetes)
> to work with the cluster from within the IDE.

### Configure Docker registry access

To retrieve images from
[GHCR](https://github.com/orgs/JetBrains-Research/packages?ecosystem=container&tab=packages&ecosystem=container&q=idegym),
you must define a dedicated `docker-registry` secret in your namespace.
After creating a [GitHub PAT](https://github.com/settings/tokens) with `read:packages` scope,
you can register the secret by running the following:

```shell
kubectl create secret docker-registry regcred \
  --docker-server=ghcr.io \
  --docker-username=username \
  --docker-password=ghp_... \
  --namespace=idegym
```

> [!TIP]
> You can verify that all the secrets were added correctly by running:
> ```shell
> kubectl get secrets -n idegym
> ```

## Populating the cluster

All Kubernetes manifests live under `orchestrator/kubernetes/` and are organized by component.
A [Kustomize](https://kustomize.io) manifest is provided to deploy everything at once:

```shell
kubectl apply -k orchestrator/kubernetes/
```

Alternatively, you can apply components individually:

```shell
# Database
kubectl apply -f orchestrator/kubernetes/postgresql/ -n idegym

# Observability
kubectl apply -f orchestrator/kubernetes/tempo/ -n idegym
kubectl apply -f orchestrator/kubernetes/prometheus/ -n idegym
kubectl apply -f orchestrator/kubernetes/grafana/ -n idegym

# Orchestrator
kubectl apply -f orchestrator/kubernetes/orchestrator/ -n idegym
```

### Expose the services

To route traffic to load balancer services running on the virtual cluster,
add the orchestrator host name to your `/etc/hosts` file:

```shell
echo "127.0.0.1 idegym.test" | sudo tee -a /etc/hosts
```

> [!NOTE]
> In the event that you delete or reset the cluster,
> this entry will still be present in your hosts file.
> As such, this step should only be performed once.

Then launch the following `minikube` service in a separate terminal window:

```shell
sudo minikube tunnel
```

> [!WARNING]
> As the command output urges:
> > Please do not close this terminal as this process must stay alive for the tunnel to be accessible...

> [!TIP]
> Check that everything is set up correctly by sending a health-check request to the orchestrator:
> ```shell
> curl idegym.test/health
> ```
> You should see a response like:
> ```json
> {"status":"healthy"}
> ```

## Deploying changes

If you made changes to the orchestrator code and would like to test them in your cluster,
first delete the existing orchestrator deployment:

```shell
kubectl delete -f orchestrator/kubernetes/orchestrator/deployment.yaml -n idegym
```

Then run the following script, which will build the orchestrator image and load it into the `minikube` image registry:

```shell
scripts/build_orchestrator_image.py
```

You can then re-deploy the orchestrator:

```shell
kubectl apply -f orchestrator/kubernetes/orchestrator/deployment.yaml -n idegym
```

The script also supports options to `--push` the build image directly to the registry, to build with `--no-cache`,
as well as perform a `--multiplatform` build.

> [!NOTE]
> If you find yourself in a situation where the cluster architecture differs from your machine,
> then additional steps need to be taken to add emulation features which would produce correct images.
> You can read more about it in [multi-platform builds](https://docs.docker.com/build/building/multi-platform).

## Accessing metrics and traces

To get an overview of collected metrics and traces, you can port-forward the Grafana service:

```shell
kubectl port-forward svc/grafana 3000:3000 -n idegym
```

Then visit http://localhost:3000 (default credentials: `admin`/`changeme`).

## Cluster cleanup

Should you choose to remove the cluster and its associated resources, executing the following will suffice:

```shell
# Delete namespace and all resources
kubectl delete namespace idegym
# Stop minikube
minikube stop
# Delete cluster (optional)
minikube delete
```
