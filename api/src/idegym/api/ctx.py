from contextvars import ContextVar
from typing import Optional

from structlog.contextvars import STRUCTLOG_KEY_PREFIX

task_id_context_var: ContextVar[Optional[str]] = ContextVar(STRUCTLOG_KEY_PREFIX + "task_id", default=None)
task_name_context_var: ContextVar[Optional[str]] = ContextVar(STRUCTLOG_KEY_PREFIX + "task_name", default=None)
