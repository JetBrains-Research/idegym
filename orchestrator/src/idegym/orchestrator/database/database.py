import asyncio
import json
from contextlib import asynccontextmanager
from typing import Any, NamedTuple, Optional, cast
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
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine  # noqa: N812
from sqlalchemy.ext.asyncio import async_sessionmaker as AsyncSessionMaker

logger = get_logger(__name__)

SessionFactory: Optional[AsyncSessionMaker[AsyncSession]] = None


class ClientNodes(NamedTuple):
    name: str
    nodes: int


async def init_db(db_url: str, config: SQLAlchemyConfig, clean_database: bool = False):
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
    if clean_database:
        logger.warning("Database cleanup requested before migrations")
        await migration_manager.clean_database()
        logger.warning("Cleaned database before migrations")

    logger.info("Running database migrations...")
    ran_migrations = await migration_manager.run_migrations()

    if not ran_migrations:
        # Another replica holds the migration lock; poll alembic_version until it catches up.
        logger.info("Waiting for migrations to complete...")
        max_wait_time = 300
        poll_interval = 1

        expected_version = migration_manager.get_expected_version()
        loop = asyncio.get_running_loop()
        start_time = loop.time()

        async with asyncio.timeout(max_wait_time):
            while True:
                await asyncio.sleep(poll_interval)
                elapsed = int(loop.time() - start_time)

                try:
                    async with migration_manager.engine.begin() as conn:
                        result = await conn.execute(text("SELECT version_num FROM alembic_version"))
                        current_version = result.scalar_one_or_none()

                        if current_version == expected_version:
                            logger.info(f"Migrations completed successfully at version {current_version}")
                            break
                        elif elapsed % 10 == 0:
                            logger.info(
                                f"Still waiting for migrations... (current: {current_version}, expected: {expected_version}, {elapsed}s elapsed)"
                            )
                except Exception:
                    if elapsed % 10 == 0:
                        logger.info(f"Still waiting for migrations... ({elapsed}s elapsed)")
                    continue

    logger.info("Database migrations completed")

    SessionFactory = AsyncSessionMaker(
        bind=db_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )

    async with get_db_session() as db:
        default_rule_query = select(ResourceLimitRule).filter(ResourceLimitRule.client_name_regex == ".*")
        default_rule_result = await db.execute(default_rule_query)
        default_rule_exists = default_rule_result.scalar_one_or_none()

        if not default_rule_exists:
            logger.info("Creating default resource limit rule ('.*')")
            await create_resource_limit_rule(
                db=db,
                client_name_regex=".*",
                pods_limit=50,
                cpu_limit=100.0,
                ram_limit=100.0,
                priority=-1,  # lowest priority — catches all clients not matched by a more specific rule
            )
        logger.info("Default resource limit rule ('.*') is present")


async def get_db():
    """Yield a database session. Intended for use with FastAPI's ``Depends``."""
    assert SessionFactory is not None, "Database engine not initialized!"
    factory = cast(AsyncSessionMaker[AsyncSession], SessionFactory)
    async with factory() as db:
        logger.debug("Getting database connection", id=id(db))
        yield db
        logger.debug("Freeing database connection", id=id(db))


@asynccontextmanager
async def get_db_session():
    """Async context-manager variant of get_db for use outside of FastAPI dependency injection."""
    assert SessionFactory is not None, "Database engine not initialized!"
    factory = cast(AsyncSessionMaker[AsyncSession], SessionFactory)
    async with factory() as db:
        logger.debug("Getting database connection", id=id(db))
        yield db
        logger.debug("Freeing database connection", id=id(db))


async def get_client(db: AsyncSession, client_id: UUID) -> Optional[Client]:
    query = select(Client).filter(Client.id == client_id)
    result = await db.execute(query)
    return result.scalar_one_or_none()


async def get_client_name(db: AsyncSession, client_id: UUID) -> Optional[str]:
    query = select(Client.name).filter(Client.id == client_id)
    result = await db.execute(query)
    return result.scalar_one_or_none()


async def get_client_by_name(db: AsyncSession, name: str) -> Optional[Client]:
    query = select(Client).filter(Client.name == name)
    result = await db.execute(query)
    return result.scalar_one_or_none()


async def get_alive_clients(db: AsyncSession) -> list[Client]:
    query = select(Client).filter(Client.availability == AvailabilityStatus.ALIVE)
    result = await db.execute(query)
    return result.scalars().all()


async def get_finished_clients(db: AsyncSession) -> list[Client]:
    query = select(Client).filter(Client.availability == AvailabilityStatus.FINISHED)
    result = await db.execute(query)
    return result.scalars().all()


async def get_clients_by_status(db: AsyncSession, statuses: set[AvailabilityStatus]) -> list[Client]:
    query = select(Client).filter(Client.availability.in_(statuses))
    result = await db.execute(query)
    return result.scalars().all()


async def create_client(db: AsyncSession, name: str, nodes_count: int = 0, namespace: str = "idegym") -> Client:
    client = Client(name=name, nodes_count=nodes_count, namespace=namespace)
    db.add(client)
    await db.commit()
    return client


async def need_to_spin_up_nodes(db: AsyncSession, client_id: UUID) -> bool:
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
    """
    Determine how many nodes should remain for the client's name after this client is removed.

    Returns None if the client doesn't exist or holds no nodes.
    Returns ClientNodes with nodes=0 if this was the last client with that name.
    Returns ClientNodes with nodes=-1 if another client with a higher count still exists (no action needed).
    Returns ClientNodes with nodes=N if the holder count should be reduced to N.
    """
    client = await get_client(db, client_id)
    if not client or client.nodes_count == 0:
        return None

    max_nodes_query = select(func.max(Client.nodes_count)).filter(
        Client.availability.in_([AvailabilityStatus.ALIVE, AvailabilityStatus.FINISHED]),
        Client.name == client.name,
        Client.id != client_id,
    )
    max_nodes_result = await db.execute(max_nodes_query)
    max_nodes = max_nodes_result.scalar()

    if max_nodes is None:
        return ClientNodes(name=client.name, nodes=0)

    if max_nodes < client.nodes_count:
        return ClientNodes(name=client.name, nodes=max_nodes)

    return ClientNodes(name=client.name, nodes=-1)


async def update_client_heartbeat(
    db: AsyncSession, client_id: UUID, availability: str = AvailabilityStatus.ALIVE
) -> Optional[Client]:
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
    query = select(IdeGYMServer).filter(IdeGYMServer.id == server_id)
    result = await db.execute(query)
    return result.scalar_one_or_none()


async def get_idegym_server_by_generated_name(db: AsyncSession, generated_name: str) -> Optional[IdeGYMServer]:
    query = select(IdeGYMServer).filter(IdeGYMServer.generated_name == generated_name)
    result = await db.execute(query)
    return result.scalar_one_or_none()


async def get_idegym_servers_by_client_id(db: AsyncSession, client_id: UUID) -> list[IdeGYMServer]:
    query = select(IdeGYMServer).filter(IdeGYMServer.client_id == client_id)
    result = await db.execute(query)
    return result.scalars().all()


async def get_running_idegym_servers(db: AsyncSession) -> list[IdeGYMServer]:
    query = select(IdeGYMServer).filter(
        IdeGYMServer.availability.in_([AvailabilityStatus.ALIVE, AvailabilityStatus.REUSED])
    )
    result = await db.execute(query)
    return result.scalars().all()


async def get_finished_idegym_servers(db: AsyncSession) -> list[IdeGYMServer]:
    query = select(IdeGYMServer).filter(IdeGYMServer.availability == AvailabilityStatus.FINISHED)
    result = await db.execute(query)
    return result.scalars().all()


async def get_idegym_servers_by_status(db: AsyncSession, statuses: set[AvailabilityStatus]) -> list[IdeGYMServer]:
    query = select(IdeGYMServer).filter(IdeGYMServer.availability.in_(statuses))
    result = await db.execute(query)
    return result.scalars().all()


async def has_pending_start_server_operations(
    db: AsyncSession,
    client_name: str,
    image_tag: str,
    container_runtime: str,
    run_as_root: bool,
    server_kind: str,
    server_name: Optional[str] = None,
    scheduled_before: Optional[int] = None,
) -> bool:
    """Check whether any SCHEDULED START_SERVER operation for matching criteria was scheduled before the given time."""
    if scheduled_before is None:
        scheduled_before = current_time_millis()

    client_result = await db.execute(select(Client).filter(Client.name == client_name))
    client = client_result.scalar_one_or_none()

    if not client:
        return False

    query = select(AsyncOperation).filter(
        AsyncOperation.request_type == AsyncOperationType.START_SERVER,
        AsyncOperation.status == AsyncOperationStatus.SCHEDULED,
        AsyncOperation.scheduled_at < scheduled_before,
        AsyncOperation.client_id == client.id,
    )

    result = await db.execute(query)
    operations = result.scalars().all()

    for op in operations:
        if op.request:
            try:
                request_data = json.loads(op.request)
                # StartServerRequest uses runtime_class_name, mapped here as container_runtime
                if (
                    request_data.get("image_tag") == image_tag
                    and request_data.get("runtime_class_name") == container_runtime
                    and request_data.get("run_as_root") == run_as_root
                    and request_data.get("server_kind") == server_kind
                ):
                    # If server_name is specified, it must match too
                    if server_name is None or request_data.get("server_name") == server_name:
                        return True
            except (json.JSONDecodeError, KeyError):
                continue

    return False


class ServerReuseLookupResult(NamedTuple):
    server: Optional[IdeGYMServer]
    blocked_by_fifo: bool


async def find_matching_finished_server(
    db: AsyncSession,
    client_name: str,
    server_name: Optional[str],
    image_tag: str,
    container_runtime: Optional[str],
    run_as_root: bool,
    server_kind: str,
    enable_fifo_check: bool = False,
) -> ServerReuseLookupResult:
    """
    Find a FINISHED server that can be reused, optionally respecting FIFO ordering.

    When enable_fifo_check is True, a SCHEDULED START_SERVER operation that was created
    before this call will block reuse and signal the caller to wait its turn in the queue.
    The selected server is locked with FOR UPDATE SKIP LOCKED to prevent two concurrent
    callers from claiming the same server.

    Returns a ServerReuseLookupResult where blocked_by_fifo=True means a matching server
    exists but this request must wait for older queued operations to proceed first.
    """

    def build_query(with_lock: bool = False):
        query = select(IdeGYMServer).filter(
            IdeGYMServer.client_name == client_name,
            IdeGYMServer.image_tag == image_tag,
            IdeGYMServer.availability == AvailabilityStatus.FINISHED,
            IdeGYMServer.container_runtime == container_runtime,
            IdeGYMServer.run_as_root == run_as_root,
            IdeGYMServer.server_kind == server_kind,
        )
        if server_name:
            query = query.filter(IdeGYMServer.server_name == server_name)

        if with_lock:
            # SKIP LOCKED lets concurrent callers each pick a different server rather than blocking.
            query = query.order_by(IdeGYMServer.last_heartbeat_time.desc()).limit(1).with_for_update(skip_locked=True)
        else:
            query = query.limit(1)

        return query

    if enable_fifo_check:
        has_pending = await has_pending_start_server_operations(
            db=db,
            client_name=client_name,
            image_tag=image_tag,
            container_runtime=container_runtime,
            run_as_root=run_as_root,
            server_kind=server_kind,
            server_name=server_name,
        )
        if has_pending:
            result = await db.execute(build_query(with_lock=False))
            has_finished_server = result.scalar_one_or_none() is not None
            return ServerReuseLookupResult(server=None, blocked_by_fifo=has_finished_server)

    result = await db.execute(build_query(with_lock=True))
    server = result.scalar_one_or_none()

    if server:
        server.last_heartbeat_time = current_time_millis()
        server.availability = AvailabilityStatus.REUSED
        await db.commit()
    return ServerReuseLookupResult(server=server, blocked_by_fifo=False)


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
    run_as_root: bool = False,
    server_kind: str = "idegym",
    service_port: int = 80,
) -> IdeGYMServer:
    # Insert first to obtain an auto-increment ID, then derive generated_name from it.
    server = IdeGYMServer(
        client_id=client_id,
        client_name=client_name,
        server_name=server_name,
        namespace=namespace,
        image_tag=image_tag,
        container_runtime=container_runtime,
        cpu=cpu,
        ram=ram,
        run_as_root=run_as_root,
        server_kind=server_kind,
        service_port=service_port,
    )
    db.add(server)
    await db.flush()  # assigns ID without committing

    base_name = server_name if server_name else "idegym-server"
    server.generated_name = f"{base_name}-{server.id}"
    await db.commit()
    return server


async def update_idegym_server_heartbeat(
    db: AsyncSession, server_id: int, availability: str = AvailabilityStatus.ALIVE
) -> Optional[IdeGYMServer]:
    server = await get_idegym_server(db, server_id)
    if not server:
        return None

    if AvailabilityStatus(server.availability).is_terminal:
        return server

    server.last_heartbeat_time = current_time_millis()
    server.availability = availability

    # Release the server's resource quota when it transitions to a terminal non-FINISHED state.
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
    matching_rule = await find_matching_resource_limit_rule(db, client_name, for_update=True)

    if matching_rule:
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
    server = await get_idegym_server(db, server_id)
    if not server:
        return None

    server.client_id = client_id
    await db.commit()
    return server


async def get_async_operation(db: AsyncSession, async_operation_id: int) -> Optional[AsyncOperation]:
    result = await db.execute(select(AsyncOperation).filter(AsyncOperation.id == async_operation_id))
    return result.scalar_one_or_none()


async def save_async_operation(
    db: AsyncSession,
    async_operation_type: AsyncOperationType,
    client_id: Optional[UUID] = None,
    server_id: Optional[int] = None,
    request: Optional[Any] = None,
) -> AsyncOperation:
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
    query = select(JobStatusRecord).filter(JobStatusRecord.job_name == job_name)
    result = await db.execute(query)
    return result.scalar_one_or_none()


async def get_job_status_by_id(db: AsyncSession, job_id: int) -> Optional[JobStatusRecord]:
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
    query = select(ResourceLimitRule).filter(ResourceLimitRule.id == rule_id)
    result = await db.execute(query)
    return result.scalar_one_or_none()


async def update_resource_limit_rule(
    db: AsyncSession, rule_id: int, pods_limit: int, cpu_limit: float, ram_limit: float, priority: int
) -> Optional[ResourceLimitRule]:
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
    Return the highest-priority ResourceLimitRule whose regex matches client_name.

    Uses PostgreSQL's ~ operator for regex matching and orders by priority descending
    so that more specific rules win over the catch-all ".*" default.
    Pass for_update=True to lock the selected row for a subsequent update.
    """
    query = (
        select(ResourceLimitRule)
        .where(func.cast(client_name, Text).op("~")(ResourceLimitRule.client_name_regex))
        .order_by(ResourceLimitRule.priority.desc())
        .limit(1)
    )

    if for_update:
        query = query.with_for_update()

    result = await db.execute(query)
    return result.scalar_one_or_none()


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
    run_as_root: bool = False,
    server_kind: str = "idegym",
    service_port: int = 80,
) -> Optional[IdeGYMServer]:
    """
    Atomically check resource limits and create a new server record.

    Locks the matching ResourceLimitRule row with FOR UPDATE to serialize concurrent
    start-server requests against the same rule, preventing over-provisioning.
    Returns None if any resource limit (CPU, RAM, or pod count) would be exceeded.
    """
    async with db.begin():
        matching_rule = await find_matching_resource_limit_rule(db, client_name, for_update=True)

        if not matching_rule:
            logger.error(f"No matching resource limit rule found for client {client_name}")
            return None

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

        matching_rule.used_cpu += cpu_request
        matching_rule.used_ram += ram_request
        matching_rule.current_pods += 1

        server = IdeGYMServer(
            client_id=client_id,
            client_name=client_name,
            server_name=server_name,
            namespace=namespace,
            image_tag=image_tag,
            container_runtime=container_runtime,
            cpu=cpu_request,
            ram=ram_request,
            run_as_root=run_as_root,
            server_kind=server_kind,
            service_port=service_port,
        )
        db.add(server)
        await db.flush()  # assigns ID without committing

        server.generated_name = f"{server_name}-{server.id}"

        # Transaction commits on context exit; rolls back on exception.
    logger.info(
        f"Created server for client {client_name} with {cpu_request} CPU, {ram_request} RAM. "
        f"Rule {matching_rule.client_name_regex} now has {matching_rule.used_cpu}/{matching_rule.cpu_limit} CPU, "
        f"{matching_rule.used_ram}/{matching_rule.ram_limit} RAM, "
        f"{matching_rule.current_pods}/{matching_rule.pods_limit} pods"
    )
    return server


async def acquire_advisory_lock(db: AsyncSession, lock_id: int) -> bool:
    """Attempt to acquire a PostgreSQL session-level advisory lock. Returns True if acquired."""
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
    """Release a PostgreSQL session-level advisory lock. Returns True if released."""
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
    """Delete completed async operations older than max_age. Returns number of deleted rows."""
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
    Mark IN_PROGRESS operations that started more than stale_inprogress ago as FINISHED_BY_WATCHER.

    This handles cases where the orchestrator restarted mid-operation and the task
    will never complete on its own. Returns the number of updated rows.
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
