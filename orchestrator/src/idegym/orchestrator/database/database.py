import asyncio
from contextlib import asynccontextmanager
from typing import Any, List, NamedTuple, Optional, Set, cast
from uuid import UUID

from idegym.api.config import SQLAlchemyConfig
from idegym.api.orchestrator.operations import AsyncOperationStatus, AsyncOperationType
from idegym.api.status import Status
from idegym.api.type import Duration
from idegym.orchestrator.database.models import (
    AsyncOperation,
    AvailabilityStatus,
    Client,
    IdeGYMServer,
    JobStatusRecord,
    ResourceLimitRule,
    current_time_millis,
)
from idegym.orchestrator.migration_manager import MigrationManager
from idegym.utils.logging import get_logger
from idegym.utils.serializer import serialize_as_json_string
from opentelemetry.instrumentation.asyncpg import AsyncPGInstrumentor
from opentelemetry.instrumentation.psycopg2 import Psycopg2Instrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from sqlalchemy import Text, delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

# noinspection PyPep8Naming
from sqlalchemy.ext.asyncio import async_sessionmaker as AsyncSessionMaker

logger = get_logger(__name__)

# Global variables
SessionFactory: Optional[AsyncSessionMaker[AsyncSession]] = None


class ClientNodes(NamedTuple):
    name: str
    nodes: int


async def init_db(db_url: str, config: SQLAlchemyConfig, clean_database: bool = False):
    """Initialize the database connection."""

    global SessionFactory
    db_engine: AsyncEngine = create_async_engine(
        url=db_url,
        **config.model_dump(),
    )
    logger.info("Connected to database", url=db_url)

    AsyncPGInstrumentor().instrument()
    Psycopg2Instrumentor().instrument()
    SQLAlchemyInstrumentor().instrument(engine=db_engine.sync_engine)

    migration_manager = MigrationManager(engine=db_engine, db_url=db_url)
    # Check if cleaning is needed before migrations
    if clean_database:
        logger.warning("Database cleanup requested before migrations")
        await migration_manager.clean_database()
        logger.warning("Cleaned database before migrations")

    # Run database migrations
    logger.info("Running database migrations...")
    ran_migrations = await migration_manager.run_migrations()

    # If another process is running migrations, wait for them to complete
    if not ran_migrations:
        logger.info("Waiting for migrations to complete...")
        max_wait_time = 300  # 5 minutes timeout
        poll_interval = 1  # Check every second
        elapsed = 0

        # Get the expected migration version from Alembic
        expected_version = migration_manager.get_expected_version()

        while elapsed < max_wait_time:
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

            # Check if migrations are complete by verifying the alembic_version table
            try:
                async with migration_manager.engine.begin() as conn:
                    result = await conn.execute(text("SELECT version_num FROM alembic_version"))
                    current_version = result.scalar_one_or_none()

                    if current_version == expected_version:
                        logger.info(f"Migrations completed successfully at version {current_version}")
                        break
                    elif elapsed % 10 == 0:  # Log every 10 seconds
                        logger.info(
                            f"Still waiting for migrations... (current: {current_version}, expected: {expected_version}, {elapsed}s elapsed)"
                        )
            except Exception:
                # Alembic version table doesn't exist yet or other error, migrations still running
                if elapsed % 10 == 0:  # Log every 10 seconds
                    logger.info(f"Still waiting for migrations... ({elapsed}s elapsed)")
                continue
        else:
            raise TimeoutError(f"Migrations did not complete within {max_wait_time} seconds")

    logger.info("Database migrations completed")

    # Note: We no longer create tables here as this will be handled by Alembic migrations

    SessionFactory = AsyncSessionMaker(
        bind=db_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )

    # Create default resource limit rule if it doesn't exist
    async with get_db_session() as db:
        default_rule_query = select(ResourceLimitRule).filter(ResourceLimitRule.client_name_regex == ".*")
        default_rule_result = await db.execute(default_rule_query)
        default_rule_exists = default_rule_result.scalar_one_or_none()

        if not default_rule_exists:
            logger.info("Creating default resource limit rule with '.*' regex during database initialization")
            await create_resource_limit_rule(
                db=db,
                client_name_regex=".*",  # Match all clients
                pods_limit=50,
                cpu_limit=100.0,
                ram_limit=100.0,
                priority=-1,  # Lowest priority
            )
        logger.info("Default resource limit rule with '.*' regex already exists or successfully created")


async def get_db():
    """
    Get a database session.
    Intended for use with FastAPI's `Depends`.
    """
    assert SessionFactory is not None, "Database engine not initialized!"
    factory = cast(AsyncSessionMaker[AsyncSession], SessionFactory)
    async with factory() as db:
        logger.debug("Getting database connection", id=id(db))
        yield db
        logger.debug("Freeing database connection", id=id(db))


@asynccontextmanager
async def get_db_session():
    """
    Get a database session.
    Intended for use with context managers:

    ```python
    async with get_db_session() as db:
        # Do something with the database session
    ```
    """
    assert SessionFactory is not None, "Database engine not initialized!"
    factory = cast(AsyncSessionMaker[AsyncSession], SessionFactory)
    async with factory() as db:
        logger.debug("Getting database connection", id=id(db))
        yield db
        logger.debug("Freeing database connection", id=id(db))


async def get_client(db: AsyncSession, client_id: UUID) -> Optional[Client]:
    """Get a client by ID."""
    query = select(Client).filter(Client.id == client_id)
    result = await db.execute(query)
    return result.scalar_one_or_none()


async def get_client_name(db: AsyncSession, client_id: UUID) -> Optional[str]:
    """Get a client name by its ID."""
    query = select(Client.name).filter(Client.id == client_id)
    result = await db.execute(query)
    return result.scalar_one_or_none()


async def get_client_by_name(db: AsyncSession, name: str) -> Optional[Client]:
    """Get a client by name."""
    query = select(Client).filter(Client.name == name)
    result = await db.execute(query)
    return result.scalar_one_or_none()


async def get_alive_clients(db: AsyncSession) -> List[Client]:
    """Get all clients with ALIVE status."""
    query = select(Client).filter(Client.availability == AvailabilityStatus.ALIVE)
    result = await db.execute(query)
    return result.scalars().all()


async def get_finished_clients(db: AsyncSession) -> List[Client]:
    """Get all clients with FINISHED status."""
    query = select(Client).filter(Client.availability == AvailabilityStatus.FINISHED)
    result = await db.execute(query)
    return result.scalars().all()


async def get_clients_by_status(db: AsyncSession, statuses: Set[AvailabilityStatus]) -> List[Client]:
    """Get all clients with status in the given list."""
    query = select(Client).filter(Client.availability.in_(statuses))
    result = await db.execute(query)
    return result.scalars().all()


async def create_client(db: AsyncSession, name: str, nodes_count: int = 0, namespace: str = "idegym") -> Client:
    """Create a new client."""
    client = Client(name=name, nodes_count=nodes_count, namespace=namespace)
    db.add(client)
    await db.commit()
    return client


async def need_to_spin_up_nodes(db: AsyncSession, client_id: UUID) -> bool:
    """Check if a client needs to spin up nodes."""
    client = await get_client(db, client_id)
    if not client:
        return False
    if client.nodes_count == 0:
        return False

    clients_query = select(Client).filter(
        Client.availability.in_([AvailabilityStatus.ALIVE, AvailabilityStatus.FINISHED]),
        Client.name == client.name,
        Client.id != client_id,
        Client.nodes_count >= client.nodes_count,
    )
    clients_result = await db.execute(clients_query)
    clients = clients_result.scalars().all()

    if len(clients) > 0:
        return False

    return True


async def need_to_release_nodes(db: AsyncSession, client_id: UUID) -> Optional[ClientNodes]:
    """Check if a client needs to release nodes."""
    client = await get_client(db, client_id)
    if not client or client.nodes_count == 0:
        return None  # Nothing should be done because the client does not exist or does not hold any nodes

    # Get maximum nodes count from other clients with the same name but different ID with ALIVE or FINISHED status
    max_nodes_query = select(func.max(Client.nodes_count)).filter(
        Client.availability.in_([AvailabilityStatus.ALIVE, AvailabilityStatus.FINISHED]),
        Client.name == client.name,
        Client.id != client_id,
    )
    max_nodes_result = await db.execute(max_nodes_query)
    max_nodes = max_nodes_result.scalar()

    if max_nodes is None:
        return ClientNodes(
            name=client.name, nodes=0
        )  # Everything should be cleaned, it was the last client with this name

    if max_nodes < client.nodes_count:
        return ClientNodes(name=client.name, nodes=max_nodes)  # Max nodes should remain

    return ClientNodes(
        name=client.name, nodes=-1
    )  # Nothing should be done because there are other clients with higher requests


async def update_client_heartbeat(
    db: AsyncSession, client_id: UUID, availability: str = AvailabilityStatus.ALIVE
) -> Optional[Client]:
    """Update a client's heartbeat time and availability."""
    client = await get_client(db, client_id)
    if not client:
        return None

    if AvailabilityStatus(client.availability).is_terminal:
        return client

    client.last_heartbeat_time = current_time_millis()
    client.availability = availability
    await db.commit()
    return client


async def get_idegym_server(db: AsyncSession, server_id: int) -> Optional[IdeGYMServer]:
    """Get an IdeGYM server by ID."""
    query = select(IdeGYMServer).filter(IdeGYMServer.id == server_id)
    result = await db.execute(query)
    return result.scalar_one_or_none()


async def get_idegym_server_by_generated_name(db: AsyncSession, generated_name: str) -> Optional[IdeGYMServer]:
    """Get an IdeGYM server by generated name."""
    query = select(IdeGYMServer).filter(IdeGYMServer.generated_name == generated_name)
    result = await db.execute(query)
    return result.scalar_one_or_none()


async def get_idegym_servers_by_client_id(db: AsyncSession, client_id: UUID) -> List[IdeGYMServer]:
    """Get all IdeGYM servers for a client."""
    query = select(IdeGYMServer).filter(IdeGYMServer.client_id == client_id)
    result = await db.execute(query)
    return result.scalars().all()


async def get_running_idegym_servers(db: AsyncSession) -> List[IdeGYMServer]:
    """Get all running IdeGYM servers (ALIVE or REUSED status)."""
    query = select(IdeGYMServer).filter(
        IdeGYMServer.availability.in_([AvailabilityStatus.ALIVE, AvailabilityStatus.REUSED])
    )
    result = await db.execute(query)
    return result.scalars().all()


async def get_finished_idegym_servers(db: AsyncSession) -> List[IdeGYMServer]:
    """Get all IdeGYM servers with FINISHED status."""
    query = select(IdeGYMServer).filter(IdeGYMServer.availability == AvailabilityStatus.FINISHED)
    result = await db.execute(query)
    return result.scalars().all()


async def get_idegym_servers_by_status(db: AsyncSession, statuses: Set[AvailabilityStatus]) -> List[IdeGYMServer]:
    """Get all IdeGYM servers with status in the given list."""
    query = select(IdeGYMServer).filter(IdeGYMServer.availability.in_(statuses))
    result = await db.execute(query)
    return result.scalars().all()


async def find_matching_finished_server(
    db: AsyncSession, client_name: str, server_name: Optional[str], image_tag: str, container_runtime: Optional[str]
) -> Optional[IdeGYMServer]:
    """Find a finished server that matches the given criteria."""
    query = select(IdeGYMServer).filter(
        IdeGYMServer.client_name == client_name,
        IdeGYMServer.image_tag == image_tag,
        IdeGYMServer.availability == AvailabilityStatus.FINISHED,
        IdeGYMServer.container_runtime == container_runtime,
    )

    # If server_name is provided, filter by it
    if server_name:
        query = query.filter(IdeGYMServer.server_name == server_name)

    # 1. Use SKIP LOCKED to prevent workers from blocking each other but lock selected rows
    # 2. Order by last_heartbeat_time to get the freshest reusable server
    query = query.order_by(IdeGYMServer.last_heartbeat_time.desc()).limit(1).with_for_update(skip_locked=True)

    # Return the first matching server (most recently finished)
    result = await db.execute(query)
    server = result.scalar_one_or_none()

    if server:
        server.last_heartbeat_time = current_time_millis()
        server.availability = AvailabilityStatus.REUSED
        await db.commit()
    return server


async def save_idegym_server(
    db: AsyncSession,
    client_id: UUID,
    client_name: str,
    server_name: Optional[str],
    namespace: str,
    image_tag: Optional[str] = None,
    container_runtime: Optional[str] = None,
    cpu: float = 0.0,
    ram: float = 0.0,
) -> IdeGYMServer:
    """Create a new IdeGYM server."""
    # Create a server without generated_name first to get an ID
    server = IdeGYMServer(
        client_id=client_id,
        client_name=client_name,
        server_name=server_name,
        namespace=namespace,
        image_tag=image_tag,
        container_runtime=container_runtime,
        cpu=cpu,
        ram=ram,
    )
    db.add(server)
    await db.flush()  # This assigns an ID but doesn't commit yet

    # Generate the name based on the pattern {client_server_name|idegym-server}-{server_id}
    base_name = server_name if server_name else "idegym-server"
    generated_name = f"{base_name}-{server.id}"

    # Update the server with the generated name
    server.generated_name = generated_name
    await db.commit()
    return server


async def update_idegym_server_heartbeat(
    db: AsyncSession, server_id: int, availability: str = AvailabilityStatus.ALIVE
) -> Optional[IdeGYMServer]:
    """Update an IdeGYM server's heartbeat time and availability."""
    server = await get_idegym_server(db, server_id)
    if not server:
        return None

    if AvailabilityStatus(server.availability).is_terminal:
        return server

    server.last_heartbeat_time = current_time_millis()
    server.availability = availability

    # If the server is being stopped or killed (but not finished), subtract its resources from the matching rule
    if availability in {
        AvailabilityStatus.STOPPED,
        AvailabilityStatus.KILLED,
        AvailabilityStatus.FAILED_TO_START,
        AvailabilityStatus.CRASHED,
        AvailabilityStatus.RESTART_FAILED,
    }:
        await subtract_resources_from_rule(db, server.client_name, server.cpu, server.ram)

    await db.commit()
    return server


async def subtract_resources_from_rule(
    db: AsyncSession, client_name: str, cpu_amount: float, ram_amount: float
) -> None:
    """Subtract resources from the matching resource limit rule."""
    # Find the matching rule and lock it with FOR UPDATE
    matching_rule = await find_matching_resource_limit_rule(db, client_name, for_update=True)

    if matching_rule:
        # Subtract the resources, ensuring we don't go below zero
        matching_rule.used_cpu = max(0.0, matching_rule.used_cpu - cpu_amount)
        matching_rule.used_ram = max(0.0, matching_rule.used_ram - ram_amount)
        matching_rule.current_pods = max(0, matching_rule.current_pods - 1)
        logger.info(
            f"Subtracted {cpu_amount} CPU and {ram_amount} RAM from rule {matching_rule.client_name_regex}. "
            f"New usage: {matching_rule.used_cpu}/{matching_rule.cpu_limit} CPU, "
            f"{matching_rule.used_ram}/{matching_rule.ram_limit} RAM, "
            f"{matching_rule.current_pods}/{matching_rule.pods_limit} pods"
        )


async def update_idegym_server_owner(db: AsyncSession, server_id: int, client_id: UUID) -> Optional[IdeGYMServer]:
    """Update an IdeGYM server's owner."""
    server = await get_idegym_server(db, server_id)
    if not server:
        return None

    server.client_id = client_id
    await db.commit()
    return server


async def get_async_operation(db: AsyncSession, async_operation_id: int) -> Optional[AsyncOperation]:
    """Get an AsyncOperation by ID."""
    result = await db.execute(select(AsyncOperation).filter(AsyncOperation.id == async_operation_id))
    return result.scalar_one_or_none()


async def save_async_operation(
    db: AsyncSession,
    async_operation_type: AsyncOperationType,
    client_id: Optional[UUID] = None,
    server_id: Optional[int] = None,
    request: Optional[Any] = None,
) -> AsyncOperation:
    """Create a new AsyncOperation record. Serializes the full request payload to JSON when possible."""
    async_operation = AsyncOperation(
        request_type=str(async_operation_type),
        status=AsyncOperationStatus.SCHEDULED,
        client_id=client_id,
        server_id=server_id,
        request=serialize_as_json_string(request),
    )
    db.add(async_operation)
    await db.commit()
    return async_operation


async def update_async_operation(
    db: AsyncSession,
    async_operation_id: int,
    async_operation_status: str,
    orchestrator_pod: Optional[str] = None,
    result: Optional[Any] = None,
) -> Optional[AsyncOperation]:
    """Update an AsyncOperation with result and status."""
    query = select(AsyncOperation).filter(AsyncOperation.id == async_operation_id).with_for_update()
    result_query = await db.execute(query)
    async_operation = result_query.scalar_one_or_none()
    if not async_operation:
        return None

    async_operation.status = async_operation_status
    async_operation.orchestrator_pod = orchestrator_pod if orchestrator_pod else async_operation.orchestrator_pod

    if result is not None:
        async_operation.result = serialize_as_json_string(result)

    if async_operation_status is AsyncOperationStatus.IN_PROGRESS and async_operation.finished_at is None:
        async_operation.started_at = current_time_millis()

    if async_operation_status in [
        AsyncOperationStatus.SUCCEEDED,
        AsyncOperationStatus.FAILED,
        AsyncOperationStatus.CANCELLED,
    ]:
        async_operation.finished_at = current_time_millis()

    await db.commit()
    return async_operation


async def get_job_status(db: AsyncSession, job_name: str) -> Optional[JobStatusRecord]:
    """Get a job status by job name."""
    query = select(JobStatusRecord).filter(JobStatusRecord.job_name == job_name)
    result = await db.execute(query)
    return result.scalar_one_or_none()


async def get_job_status_by_id(db: AsyncSession, job_id: int) -> Optional[JobStatusRecord]:
    """Get a job status by ID."""
    query = select(JobStatusRecord).filter(JobStatusRecord.id == job_id)
    result = await db.execute(query)
    return result.scalar_one_or_none()


async def save_job_status(
    db: AsyncSession,
    job_name: str,
    tag: str,
    status: str = Status.IN_PROGRESS,
    details: Optional[str] = None,
    request_id: Optional[str] = None,
) -> JobStatusRecord:
    """Create a new job status record."""
    # Check if a record with this job_name already exists
    existing_record = await get_job_status(db, job_name)
    if existing_record:
        return await update_job_status(db, job_name, status, tag, details, request_id)

    job_status = JobStatusRecord(job_name=job_name, status=status, details=details, tag=tag, request_id=request_id)
    db.add(job_status)
    await db.commit()
    return job_status


async def update_job_status(
    db: AsyncSession,
    job_name: str,
    status: str,
    tag: str,
    details: Optional[str] = None,
    request_id: Optional[str] = None,
) -> Optional[JobStatusRecord]:
    """Update a job status record."""
    job_status = await get_job_status(db, job_name)
    if not job_status:
        return None

    job_status.tag = tag
    job_status.status = status
    job_status.updated_at = current_time_millis()

    if details is not None:
        job_status.details = details

    if request_id is not None:
        job_status.request_id = request_id

    await db.commit()
    return job_status


async def create_resource_limit_rule(
    db: AsyncSession,
    client_name_regex: str,
    pods_limit: int,
    cpu_limit: float,
    ram_limit: float,
    priority: int = 0,
    used_cpu: float = 0.0,
    used_ram: float = 0.0,
    current_pods: int = 0,
) -> ResourceLimitRule:
    """Create a new resource limit rule."""
    rule = ResourceLimitRule(
        client_name_regex=client_name_regex,
        pods_limit=pods_limit,
        cpu_limit=cpu_limit,
        ram_limit=ram_limit,
        used_cpu=used_cpu,
        used_ram=used_ram,
        current_pods=current_pods,
        priority=priority,
    )
    db.add(rule)
    await db.commit()
    return rule


async def get_resource_limit_rule(db: AsyncSession, rule_id: int) -> Optional[ResourceLimitRule]:
    """Get a resource limit rule by ID."""
    query = select(ResourceLimitRule).filter(ResourceLimitRule.id == rule_id)
    result = await db.execute(query)
    return result.scalar_one_or_none()


async def update_resource_limit_rule(
    db: AsyncSession, rule_id: int, pods_limit: int, cpu_limit: float, ram_limit: float, priority: int
) -> Optional[ResourceLimitRule]:
    """Update a resource limit rule."""
    rule = await get_resource_limit_rule(db, rule_id)
    if not rule:
        return None

    rule.pods_limit = pods_limit
    rule.cpu_limit = cpu_limit
    rule.ram_limit = ram_limit
    rule.priority = priority

    await db.commit()
    return rule


async def find_matching_resource_limit_rule(
    db: AsyncSession, client_name: str, for_update: bool = False
) -> Optional[ResourceLimitRule]:
    """
    Find a resource limit rule that matches the client name.

    Args:
        db: The database session
        client_name: The client name to match against the regex patterns
        for_update: If True, the matching rule will be locked with FOR UPDATE

    Returns:
        The matching resource limit rule, or None if no match is found
    """
    # Use PostgreSQL's regex operator to find matching rules directly in SQL
    # The ~ operator performs regex matching in PostgreSQL
    # We order by priority in descending order to get the highest priority rule first
    query = (
        select(ResourceLimitRule)
        .where(func.cast(client_name, Text).op("~")(ResourceLimitRule.client_name_regex))
        .order_by(ResourceLimitRule.priority.desc())
        .limit(1)
    )

    # Add FOR UPDATE if needed
    if for_update:
        query = query.with_for_update()

    result = await db.execute(query)
    matching_rule = result.scalar_one_or_none()

    return matching_rule


async def check_resources_and_save_server(
    db: AsyncSession,
    client_id: UUID,
    client_name: str,
    server_name: str,
    namespace: str,
    cpu_request: float,
    ram_request: float,
    image_tag: Optional[str] = None,
    container_runtime: Optional[str] = None,
) -> Optional[IdeGYMServer]:
    """
    Check resource availability and save a new server in a single atomic transaction.
    This prevents race conditions when multiple requests check for resources and create servers simultaneously.

    The function selects the appropriate rule with FOR UPDATE to lock just that row, ensuring
    that other transactions trying to use the same rule will wait until this transaction completes.
    This approach is more efficient than locking the entire table.
    """
    # Start an explicit transaction
    async with db.begin():
        # Find the matching rule for this client and lock it with FOR UPDATE
        matching_rule = await find_matching_resource_limit_rule(db, client_name, for_update=True)

        if not matching_rule:
            logger.error(f"No matching resource limit rule found for client {client_name}")
            return None

        # Check if adding the new server would exceed any limits
        if matching_rule.used_cpu + cpu_request > matching_rule.cpu_limit:
            logger.warning(
                f"Client {client_name} has reached CPU limit: "
                f"{matching_rule.used_cpu + cpu_request}/{matching_rule.cpu_limit}"
            )
            return None

        if matching_rule.used_ram + ram_request > matching_rule.ram_limit:
            logger.warning(
                f"Client {client_name} has reached RAM limit: "
                f"{matching_rule.used_ram + ram_request}/{matching_rule.ram_limit}"
            )
            return None

        if matching_rule.current_pods + 1 > matching_rule.pods_limit:
            logger.warning(
                f"Client {client_name} has reached pod limit: "
                f"{matching_rule.current_pods + 1}/{matching_rule.pods_limit}"
            )
            return None

        # Update the used resources in the rule
        matching_rule.used_cpu += cpu_request
        matching_rule.used_ram += ram_request
        matching_rule.current_pods += 1

        # Create a server without generated_name first to get an ID
        server = IdeGYMServer(
            client_id=client_id,
            client_name=client_name,
            server_name=server_name,
            namespace=namespace,
            image_tag=image_tag,
            container_runtime=container_runtime,
            cpu=cpu_request,
            ram=ram_request,
        )
        db.add(server)
        await db.flush()  # This assigns an ID but doesn't commit yet

        # Generate the name based on the pattern {client_server_name|idegym-server}-{server_id}
        base_name = server_name
        generated_name = f"{base_name}-{server.id}"

        # Update the server with the generated name
        server.generated_name = generated_name

        # The transaction will be committed automatically when the context manager exits
        # If any exception occurs, the transaction will be rolled back automatically
    logger.info(
        f"Created server for client {client_name} with {cpu_request} CPU, {ram_request} RAM. "
        f"Rule {matching_rule.client_name_regex} now has {matching_rule.used_cpu}/{matching_rule.cpu_limit} CPU, "
        f"{matching_rule.used_ram}/{matching_rule.ram_limit} RAM, "
        f"{matching_rule.current_pods}/{matching_rule.pods_limit} pods"
    )
    return server


async def acquire_advisory_lock(db: AsyncSession, lock_id: int) -> bool:
    """
    Attempt to acquire a PostgreSQL advisory lock.

    Args:
        db: Database session
        lock_id: Unique identifier for the lock

    Returns:
        bool: True if lock was acquired, False if already taken
    """
    try:
        result = await db.execute(text("SELECT pg_try_advisory_lock(:lock_id)"), {"lock_id": lock_id})
        acquired = result.scalar()
        if acquired:
            logger.info(f"Successfully acquired advisory lock {lock_id}")
        else:
            logger.info(f"Advisory lock {lock_id} is already taken by another process")
        return acquired
    except Exception as e:
        logger.error(f"Failed to acquire advisory lock {lock_id}: {e}")
        return False


async def release_advisory_lock(db: AsyncSession, lock_id: int) -> bool:
    """
    Release a PostgreSQL advisory lock.

    Args:
        db: Database session
        lock_id: Unique identifier for the lock

    Returns:
        bool: True if lock was released, False otherwise
    """
    try:
        result = await db.execute(text("SELECT pg_advisory_unlock(:lock_id)"), {"lock_id": lock_id})
        released = result.scalar()
        if released:
            logger.info(f"Successfully released advisory lock {lock_id}")
        else:
            logger.warning(f"Failed to release advisory lock {lock_id} - may not have been held by this session")
        return released
    except Exception as e:
        logger.error(f"Failed to release advisory lock {lock_id}: {e}")
        return False


async def delete_old_async_operations(db: AsyncSession, current_time: int, max_age: Duration) -> int:
    """
    Delete async operations with started_at earlier than (current_time - max_age).

    Args:
        db: Database session
        current_time: Current time in milliseconds
        max_age: Duration (timedelta) specifying maximum age of requests

    Returns the number of deleted async operations.
    """
    try:
        max_age_ms = int(max_age.total_seconds() * 1000)
        result = await db.execute(delete(AsyncOperation).where(AsyncOperation.started_at < (current_time - max_age_ms)))
        deleted_count = result.rowcount or 0
        await db.commit()
        logger.info(f"Deleted {deleted_count} async operations older than {max_age}")
        return deleted_count
    except Exception:
        logger.exception("Error deleting old async operations")
        await db.rollback()
        return 0


async def mark_stale_async_operations_as_finished(
    db: AsyncSession, current_time: int, stale_inprogress: Duration
) -> int:
    """
    Mark IN_PROGRESS async operations started before (current_time - stale_inprogress) as FINISHED_BY_WATCHER and set finished_at.

    Args:
        db: Database session
        current_time: Current time in milliseconds
        stale_inprogress: Duration (timedelta) specifying how long a stale async operations should be IN_PROGRESS before marked as finished by watcher

    Returns the number of updated async operations.
    """
    try:
        stale_inprogress_ms = int(stale_inprogress.total_seconds() * 1000)
        result = await db.execute(
            update(AsyncOperation)
            .where(
                AsyncOperation.started_at < (current_time - stale_inprogress_ms),
                AsyncOperation.status == AsyncOperationStatus.IN_PROGRESS,
            )
            .values(status=AsyncOperationStatus.FINISHED_BY_WATCHER, finished_at=current_time)
        )
        updated_count = result.rowcount or 0
        if updated_count:
            await db.commit()
        logger.info(f"Marked {updated_count} stale IN_PROGRESS async operations as FINISHED_BY_WATCHER")
        return updated_count
    except Exception:
        logger.exception("Error marking stale async operations as FINISHED_BY_WATCHER")
        await db.rollback()
        return 0
