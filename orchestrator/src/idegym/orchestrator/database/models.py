import time
from uuid import uuid4

from idegym.api.orchestrator.clients import AvailabilityStatus
from idegym.api.orchestrator.operations import AsyncOperationStatus
from idegym.api.status import Status
from sqlalchemy import BigInteger, Boolean, Column, Float, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.orm import DeclarativeBase


def current_time_millis():
    return int(time.time() * 1000)


class Base(AsyncAttrs, DeclarativeBase):
    pass


class Client(Base):
    __tablename__ = "clients"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    name = Column(String, index=True)
    namespace = Column(String, default="idegym")

    created_at = Column(BigInteger, default=current_time_millis)
    last_heartbeat_time = Column(BigInteger, default=current_time_millis)
    availability = Column(String, default=AvailabilityStatus.ALIVE)

    nodes_count = Column(BigInteger, default=0)


class IdeGYMServer(Base):
    __tablename__ = "servers"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    client_id = Column(UUID(as_uuid=True), ForeignKey("clients.id"), nullable=False)
    client_name = Column(String)

    server_name = Column(String)
    generated_name = Column(String, index=True, unique=True)
    namespace = Column(String, default="idegym")

    created_at = Column(BigInteger, default=current_time_millis)
    last_heartbeat_time = Column(BigInteger, default=current_time_millis)
    availability = Column(String, default=AvailabilityStatus.ALIVE)

    image_tag = Column(String, nullable=True)
    container_runtime = Column(String, nullable=True)
    cpu = Column(Float, default=0.0)  # cores
    ram = Column(Float, default=0.0)  # GB
    run_as_root = Column(Boolean, default=False, nullable=False)
    server_kind = Column(String, default="idegym", nullable=False)
    service_port = Column(Integer, default=80, nullable=False)


class ResourceLimitRule(Base):
    __tablename__ = "resource_limit_rules"

    id = Column(BigInteger, primary_key=True, autoincrement=True)

    client_name_regex = Column(String, index=True, unique=True, nullable=False)
    pods_limit = Column(Integer, nullable=False)
    cpu_limit = Column(Float, nullable=False)  # cores
    ram_limit = Column(Float, nullable=False)  # GB
    used_cpu = Column(Float, default=0.0, nullable=False)
    used_ram = Column(Float, default=0.0, nullable=False)
    current_pods = Column(Integer, default=0, nullable=False)
    priority = Column(Integer, default=0, nullable=False)  # higher priority rules win over lower ones


class JobStatusRecord(Base):
    __tablename__ = "job_statuses"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    job_name = Column(String, index=True, unique=True, nullable=False)

    details = Column(Text, nullable=True)
    tag = Column(String, nullable=False)
    request_id = Column(String, nullable=True)

    status = Column(String, default=Status.IN_PROGRESS)
    created_at = Column(BigInteger, default=current_time_millis)
    updated_at = Column(BigInteger, default=current_time_millis)


class AsyncOperation(Base):
    __tablename__ = "async_operations"

    id = Column(BigInteger, primary_key=True, autoincrement=True)

    request_type = Column(String, nullable=False)
    status = Column(String, default=AsyncOperationStatus.SCHEDULED)

    request = Column(Text, nullable=True)
    result = Column(Text, nullable=True)

    client_id = Column(UUID(as_uuid=True), ForeignKey("clients.id"))
    server_id = Column(BigInteger, ForeignKey("servers.id"), nullable=True)

    orchestrator_pod = Column(String, nullable=True)

    scheduled_at = Column(BigInteger, default=current_time_millis)
    started_at = Column(BigInteger, nullable=True)
    finished_at = Column(BigInteger, nullable=True)
