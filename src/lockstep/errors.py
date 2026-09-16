from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from lockstep.core.exceptions import (
    AuthError,
    IngestionError,
    LockstepError,
    NotFoundError,
    ValidationError,
)

_STATUS_BY_EXCEPTION: dict[type[LockstepError], int] = {
    NotFoundError: status.HTTP_404_NOT_FOUND,
    ValidationError: status.HTTP_400_BAD_REQUEST,
    IngestionError: status.HTTP_400_BAD_REQUEST,
    AuthError: status.HTTP_401_UNAUTHORIZED,
}


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(LockstepError)
    async def handle_lockstep_error(request: Request, exc: LockstepError) -> JSONResponse:
        status_code = _STATUS_BY_EXCEPTION.get(type(exc), status.HTTP_400_BAD_REQUEST)
        return JSONResponse(status_code=status_code, content={"detail": str(exc)})
