from fastapi import APIRouter, HTTPException, status
from idegym.api.orchestrator.operations import AsyncOperationStatusResponse
from idegym.orchestrator.database.helpers import find_async_operation
from idegym.orchestrator.util.decorators import handle_general_exceptions
from idegym.utils.logging import get_logger

router = APIRouter()
logger = get_logger(__name__)


@router.get("/api/operations/status/{operation_id}")
@handle_general_exceptions(error_message="Failed to get async operation status")
async def get_operation_status(operation_id: int):
    """Check status of an async operation."""
    async_operation = await find_async_operation(operation_id)
    if not async_operation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Operation with ID {operation_id} not found")
    return AsyncOperationStatusResponse.model_validate(async_operation, from_attributes=True)
