import json
import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.helpers.model import MapArea, MapJobAcceptedResponse, MapJobStatusResponse
from app.services.jobs import build_default_job_service


def create_app(service=None, api_key: str | None = None) -> FastAPI:
    configured_api_key = api_key or os.getenv("API_KEY")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.map_service = service or build_default_job_service()
        app.state.api_key = configured_api_key
        yield

    app = FastAPI(lifespan=lifespan)
    app.state.map_service = service
    app.state.api_key = configured_api_key
    bearer_scheme = HTTPBearer(auto_error=False)

    def get_service(request: Request):
        current = getattr(request.app.state, "map_service", None)
        if current is None:
            current = build_default_job_service()
            request.app.state.map_service = current
        return current

    def require_api_key(
        request: Request,
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    ) -> None:
        expected = getattr(request.app.state, "api_key", None)
        token = credentials.credentials if credentials else None
        if not expected or token != expected:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid api key",
            )

    @app.post(
        "/api/map",
        response_model=MapJobAcceptedResponse,
        status_code=202,
        dependencies=[Depends(require_api_key)],
    )
    async def vector_map(area: MapArea, request: Request):
        try:
            return get_service(request).submit_job(area)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get(
        "/api/map/jobs/{job_id}",
        response_model=MapJobStatusResponse,
        dependencies=[Depends(require_api_key)],
    )
    async def vector_map_job_status(job_id: str, request: Request):
        try:
            return get_service(request).get_job(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="job not found") from exc

    @app.post("/api/map/stream", dependencies=[Depends(require_api_key)])
    async def vector_map_stream(area: MapArea, request: Request):
        service = get_service(request)

        def generate():
            for event in service.submit_and_stream(area):
                yield json.dumps(event) + "\n"

        return StreamingResponse(generate(), media_type="application/x-ndjson")

    @app.get("/api/map/jobs/{job_id}/stream", dependencies=[Depends(require_api_key)])
    async def vector_map_job_stream(job_id: str, request: Request):
        service = get_service(request)

        def generate():
            try:
                for event in service.stream_job(job_id):
                    yield json.dumps(event) + "\n"
            except KeyError as exc:
                yield json.dumps({"event": "error", "detail": "job not found", "status_code": 404}) + "\n"

        return StreamingResponse(generate(), media_type="application/x-ndjson")

    return app


app = create_app()
