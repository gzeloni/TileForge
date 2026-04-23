"""FastAPI application setup."""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.routes import router
from app.services.jobs import job_service
from app.services.sync import source_sync


def make_app(service=None, api_key: str | None = None) -> FastAPI:
    """Create the FastAPI application."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """Initialize and release runtime services."""

        app.state.jobs = service or job_service()
        app.state.api_key = api_key
        app.state.sync = None if service is not None else source_sync()
        if app.state.sync is not None:
            app.state.sync.start()
        try:
            yield
        finally:
            if app.state.sync is not None:
                app.state.sync.stop()

    app = FastAPI(root_path="/homelander", lifespan=lifespan)
    app.state.jobs = service
    app.state.api_key = api_key
    app.state.sync = None
    app.include_router(router)
    return app


app = make_app()
