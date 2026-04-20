import json
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.helpers.model import MapArea, MapJobAcceptedResponse, MapJobStatusResponse
from app.services.jobs import build_default_job_service


def create_app(service=None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.map_service = service or build_default_job_service()
        yield

    app = FastAPI(lifespan=lifespan)
    app.state.map_service = service

    def get_service(request: Request):
        current = getattr(request.app.state, "map_service", None)
        if current is None:
            current = build_default_job_service()
            request.app.state.map_service = current
        return current

    @app.post("/api/map", response_model=MapJobAcceptedResponse, status_code=202)
    async def vector_map(area: MapArea, request: Request):
        try:
            return get_service(request).submit_job(area)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/api/map/jobs/{job_id}", response_model=MapJobStatusResponse)
    async def vector_map_job_status(job_id: str, request: Request):
        try:
            return get_service(request).get_job(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="job not found") from exc

    @app.post("/api/map/stream")
    async def vector_map_stream(area: MapArea, request: Request):
        service = get_service(request)

        def generate():
            for event in service.submit_and_stream(area):
                yield json.dumps(event) + "\n"

        return StreamingResponse(generate(), media_type="application/x-ndjson")

    @app.get("/api/map/jobs/{job_id}/stream")
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
