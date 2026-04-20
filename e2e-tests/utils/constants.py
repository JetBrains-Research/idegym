"""Shared constants for e2e testing."""

# Kubernetes configuration
DEFAULT_NAMESPACE = "idegym-local"
KUBE_SYSTEM_NAMESPACE = "kube-system"
INGRESS_NAMESPACE = "ingress-nginx"
INGRESS_CONTROLLER_SERVICE = "ingress-nginx-controller"

# Kaniko job names
REGISTRY_PUSH_JOB_NAME = "registry-push-job"
REGISTRY_PULL_JOB_NAME = "registry-pull-job"

# Registry configuration
# On GitHub Actions the minikube registry addon exposes the registry on localhost:5000 of the node,
# so containerd (ctr) must pull via that address.
PUSH_LOCAL_REGISTRY_HOST = "registry.kube-system.svc.cluster.local"
PULL_LOCAL_REGISTRY_HOST = "localhost:5000"
MINIKUBE_NODE_NAME = "minikube"

# URLs
BASE_URL = "http://idegym-local.test"

# Timeouts (in seconds)
DEFAULT_REQUEST_TIMEOUT = 60
DEFAULT_SERVER_START_TIMEOUT = 600
DEFAULT_COMMAND_TIMEOUT = 60
DEFAULT_HEALTH_CHECK_TIMEOUT = 300
DEFAULT_NAMESPACE_DELETE_TIMEOUT = 180
DEFAULT_POD_DELETE_TIMEOUT = 120
DEFAULT_POD_READY_TIMEOUT = 120

# Check intervals (in seconds)
DEFAULT_CHECK_INTERVAL = 2
HEALTH_CHECK_INTERVAL = 10

# Pod labels
ORCHESTRATOR_APP_LABEL = "orchestrator"

# PostgreSQL
POSTGRESQL_APP_LABEL = "postgresql"
POSTGRESQL_USER = "postgres"
POSTGRESQL_DB = "idegym"
