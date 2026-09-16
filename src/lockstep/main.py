from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from lockstep.api.v1 import auth, invoices, runs, vendors
from lockstep.config import get_settings
from lockstep.errors import register_exception_handlers

settings = get_settings()


def create_app() -> FastAPI:
    app = FastAPI(title="Lockstep API", version="0.1.0")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    register_exception_handlers(app)

    app.include_router(auth.router, prefix=settings.api_v1_prefix)
    app.include_router(runs.router, prefix=settings.api_v1_prefix)
    app.include_router(invoices.router, prefix=settings.api_v1_prefix)
    app.include_router(vendors.router, prefix=settings.api_v1_prefix)

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    return app


app = create_app()
