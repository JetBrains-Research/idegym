# IdeGYM Orchestrator

The IdeGYM Orchestrator is a FastAPI-based REST API server that manages Kubernetes deployments, forwards requests to these deployments, and manages clients and servers in PostgreSQL.

## Features

- Start and stop Kubernetes deployments with configurable parameters
- Forward requests to deployments and stream responses back
- Register and manage clients with heartbeat tracking
- Track IdeGYM server status and requests
- Kubernetes deployment configuration for the orchestrator and PostgreSQL

## API Endpoints

### Health Check

```
GET /health
```

Returns a simple health check response to verify the orchestrator is running.

### Client Management

```
POST /api/clients
```

Register a new client in the database.

**Request Body:**
```json
{
  "name": "client-name"
}
```

**Response:**
```json
{
  "id": "uuid",
  "name": "client-name",
  "last_heartbeat_time": 1234567890123,
  "availability": "ALIVE",
  "created_at": 1234567890123
}
```

```
GET /api/clients
```

List all registered clients.

```
POST /api/clients/heartbeat
```

Send a heartbeat for a client to update its status.

**Request Body:**
```json
{
  "client_id": "uuid",
  "availability": "ALIVE"
}
```

```
DELETE /api/clients
```

Finish working with a client, stopping all its servers and marking it as finished.

**Request Body:**
```json
{
  "client_id": "uuid",
  "namespace": "idegym"
}
```

**Response:**
```json
{
  "id": "uuid",
  "name": "client-name",
  "last_heartbeat_time": 1234567890123,
  "availability": "FINISHED",
  "created_at": 1234567890123
}
```

### IdeGYM Server Management

```
POST /api/idegym-servers
```

Start a Kubernetes deployment for an IdeGYM server.

**Request Body:**
```json
{
  "client_id": "uuid",
  "namespace": "idegym",
  "image_tag": "your-image:tag",
  "server_name": "your-server-name",  // Optional, will be used as prefix for generated name
  "runtime_class_name": null,
  "run_as_root": false,
  "service_port": 80,
  "container_port": 8000,
  "resources": null,
  "server_start_wait_timeout": 60
}
```

**Response:**
```json
{
  "namespace": "idegym",
  "client_id": "uuid",
  "server_id": 1,  // Integer, auto-incrementing
  "server_name": "your-server-name",  // Optional, may be null
  "generated_name": "your-server-name-1",  // Automatically generated name
  "service_name": "your-server-name-1-service"
}
```

```
DELETE /api/idegym-servers
```

Stop a Kubernetes deployment for an IdeGYM server.

**Request Body:**
```json
{
  "client_id": "uuid",
  "namespace": "idegym",
  "server_id": 1  // Integer ID of the server to stop
}
```

```
POST /api/idegym-servers/restart
```

Restart pods for an IdeGYM server without deleting the deployment or service.

**Request Body:**
```json
{
  "client_id": "uuid",
  "namespace": "idegym",
  "server_id": 1,  // Integer ID of the server to restart
  "server_start_wait_timeout": 60  // Timeout in seconds for waiting for pods to be ready
}
```

### Request Forwarding

```
ANY /api/forward/{client_id}/{server_name}/{path}
```

Forward a request to an IdeGYM server. The method, headers, and body of the original request are preserved.

### IdeGYM Server Requests

```
GET /api/idegym-servers/requests/{server_name}
```

Get all requests for an IdeGYM server.

## Database Models

### Client

- `id`: UUID (primary key)
- `name`: String (optional)
- `last_heartbeat_time`: BigInteger (timestamp in milliseconds)
- `availability`: String (ALIVE, FINISHED, DEAD)
- `created_at`: BigInteger (timestamp in milliseconds)

### IdeGYM Server

- `id`: UUID (primary key)
- `client_id`: UUID (foreign key to Client)
- `server_name`: String (unique)
- `last_heartbeat_time`: BigInteger (timestamp in milliseconds)
- `availability`: String (ALIVE, FINISHED, DEAD)
- `created_at`: BigInteger (timestamp in milliseconds)

### IdeGYM Request

- `id`: UUID (primary key)
- `server_name`: String (foreign key to IdeGYM Server)
- `request`: Text
- `started_at`: BigInteger (timestamp in milliseconds)
- `result`: Text (optional)
- `finished_at`: BigInteger (timestamp in milliseconds, optional)
- `status`: String (IN_PROGRESS, SUCCESS, FAIL)

## Deployment

### Prerequisites

- Kubernetes cluster
- kubectl configured to access your cluster
- Docker for building the image

### Building the Image

From the repository root:

```bash
docker build -f orchestrator/Dockerfile -t ghcr.io/jetbrains-research/idegym/orchestrator:latest .
```

### Deploying to Kubernetes

To apply the manifests:

```bash
kubectl apply -f orchestrator/kubernetes/postgresql/ -n idegym
kubectl apply -f orchestrator/kubernetes/orchestrator/ -n idegym
```

This will deploy:
- PostgreSQL database as a StatefulSet with persistent storage
- IdeGYM Orchestrator with its service account and RBAC configuration

See `documentation/local_deployment.md` for the full local setup guide including observability tools.

## Environment Variables

The orchestrator uses the following environment variables for PostgreSQL connection:

- `POSTGRES_HOST`: PostgreSQL host (default: "localhost")
- `POSTGRES_PORT`: PostgreSQL port (default: "5432")
- `POSTGRES_USER`: PostgreSQL user (default: "postgres")
- `POSTGRES_PASSWORD`: PostgreSQL password (default: "postgres")
- `POSTGRES_DB`: PostgreSQL database name (default: "idegym")

## Connecting to Orchestrator from Outside

To connect to the orchestrator from outside the Kubernetes cluster, you need to set up authentication using environment variables in your Kubernetes YAML files. The orchestrator uses Basic Authentication, which requires a username and password.

Add the following environment variables to your Kubernetes deployment or pod configuration:

```yaml
env:
  - name: IDEGYM_AUTH_USERNAME
    valueFrom:
      secretKeyRef:
        name: idegym-basic-auth
        key: username
  - name: IDEGYM_AUTH_PASSWORD
    valueFrom:
      secretKeyRef:
        name: idegym-basic-auth
        key: password
```

These environment variables are used to authenticate requests to the orchestrator. Here's an example of how to use them in Python code:

```python
import os
import http.client
import ssl
import base64
import json

# Create Basic Auth header from environment variables
auth = base64.b64encode(f'{os.environ["IDEGYM_AUTH_USERNAME"]}:{os.environ["IDEGYM_AUTH_PASSWORD"]}'.encode()).decode()

# Connect to the orchestrator
conn = http.client.HTTPSConnection('idegym.labs.jb.gg', context=ssl._create_unverified_context())

# Example request data
client_data = {
    "name": "Example Client"
}

# Make a request with authentication
conn.request(
    method='POST',
    url='/api/clients',
    headers={
        'Authorization': f'Basic {auth}',
        'Content-Type': 'application/json'
    },
    body=json.dumps(client_data)
)

# Get and print the response
resp = conn.getresponse()
print(resp.status, resp.reason)
print(resp.read())
```

## Development

### Running Locally

1. Install dependencies:
```bash
pip install -e ./orchestrator -e ./api -e ./common-utils
```

2. Run the orchestrator:
```bash
python -m idegym.orchestrator.main
```

### Testing

To test the orchestrator:

1. Start the orchestrator
2. Use curl or any HTTP client to make requests to the API endpoints

### Kubernetes Client

The orchestrator uses the Kubernetes Python client to create and manage deployments and services. The client is initialized to use either in-cluster configuration (when running inside Kubernetes) or local configuration (when running locally).

Key functions:
- `deploy_server`: Creates a Kubernetes deployment and service for an IdeGYM server
- `delete_service_and_deployment`: Deletes a Kubernetes deployment and service
- `wait_for_pods_ready`: Waits for pods to be ready
- `pods_are_ready`: Checks if pods are ready
