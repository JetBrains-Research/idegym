"""Shared constants for e2e testing."""

# Kubernetes configuration
DEFAULT_NAMESPACE = "idegym-local"
INGRESS_NAMESPACE = "ingress-nginx"
INGRESS_CONTROLLER_SERVICE = "ingress-nginx-controller"

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
