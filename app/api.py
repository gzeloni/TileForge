import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.helpers.model import (
    ArtifactPaths,
    MapArea,
    MapJobAcceptedResponse,
    MapJobStatusResponse,
    MapPackageAcceptedResponse,
    MapPackageStatusResponse,
)
from app.services.jobs import build_default_job_service


def create_app(service=None, api_key: str | None = None) -> FastAPI:
    configured_api_key = api_key or os.getenv("API_KEY")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.map_service = service or build_default_job_service()
        app.state.api_key = configured_api_key
        yield

    app = FastAPI(
        root_path="/homelander",
        lifespan=lifespan)
    app.state.map_service = service
    app.state.api_key = configured_api_key
    bearer_scheme = HTTPBearer(auto_error=False)

    def get_service(request: Request):
        current = getattr(request.app.state, "map_service", None)
        if current is None:
            current = build_default_job_service()
            request.app.state.map_service = current
        return current

    def package_artifact_links(package_id: str) -> ArtifactPaths:
        return ArtifactPaths(
            region_pbf=f"/api/map/packages/{package_id}/artifacts/region_pbf",
            mbtiles=f"/api/map/packages/{package_id}/artifacts/mbtiles",
            graph=f"/api/map/packages/{package_id}/artifacts/graph",
            meta=f"/api/map/packages/{package_id}/artifacts/meta",
        )

    def package_response(payload: dict) -> MapPackageStatusResponse:
        package_id = payload["package_id"]
        artifacts = package_artifact_links(package_id)
        return MapPackageStatusResponse(
            package_id=package_id,
            status=payload["status"],
            source_id=payload["source_id"],
            job_id=payload.get("job_id"),
            error=payload.get("error"),
            expires_at=payload.get("expires_at"),
            artifacts=artifacts,
        )

    def package_accepted_response(payload: dict) -> MapPackageAcceptedResponse:
        package_id = payload["package_id"]
        return MapPackageAcceptedResponse(
            package_id=package_id,
            status=payload["status"],
            source_id=payload["source_id"],
            job_id=payload.get("job_id"),
            expires_at=payload.get("expires_at"),
            artifacts=package_artifact_links(package_id),
        )

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

    @app.post(
        "/api/map/packages",
        response_model=MapPackageAcceptedResponse,
        status_code=202,
        dependencies=[Depends(require_api_key)],
    )
    async def vector_map_package(area: MapArea, request: Request):
        try:
            return package_accepted_response(get_service(request).submit_package(area))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get(
        "/api/map/packages/{package_id}",
        response_model=MapPackageStatusResponse,
        dependencies=[Depends(require_api_key)],
    )
    async def vector_map_package_status(package_id: str, request: Request):
        try:
            return package_response(get_service(request).get_package(package_id))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="package not found") from exc

    @app.get(
        "/api/map/packages/{package_id}/artifacts/{artifact_name}",
        dependencies=[Depends(require_api_key)],
    )
    async def vector_map_package_artifact(package_id: str, artifact_name: str, request: Request):
        try:
            path = get_service(request).get_package_artifact_path(package_id, artifact_name)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except KeyError as exc:
            detail = "artifact not found" if str(exc).strip("'") == f"artifact:{artifact_name}" else "package not found"
            raise HTTPException(status_code=404, detail=detail) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="artifact file not found") from exc

        return FileResponse(path)

    @app.get(
        "/api/map/packages/{package_id}/stream",
        dependencies=[Depends(require_api_key)],
    )
    async def vector_map_package_stream(package_id: str, request: Request):
        service = get_service(request)

        def generate():
            try:
                for event in service.stream_package(package_id):
                    yield json.dumps(event) + "\n"
            except KeyError:
                yield json.dumps({"event": "error", "detail": "package not found", "status_code": 404}) + "\n"

        return StreamingResponse(generate(), media_type="application/x-ndjson")

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

    @app.get(
        "/api/map/jobs/{job_id}/artifacts/{artifact_name}",
        dependencies=[Depends(require_api_key)],
    )
    async def vector_map_job_artifact(job_id: str, artifact_name: str, request: Request):
        job = get_service(request).get_job(job_id)
        if job.result is None:
            raise HTTPException(status_code=409, detail="job result is not available")

        artifacts = job.result.artifacts
        artifact_paths = {
            "mbtiles": artifacts.mbtiles,
            "graph": artifacts.graph,
            "meta": artifacts.meta,
            "region_pbf": artifacts.region_pbf,
        }
        artifact_path = artifact_paths.get(artifact_name)
        if artifact_path is None:
            raise HTTPException(status_code=404, detail="artifact not found")

        path = Path(artifact_path)
        if not path.exists():
            raise HTTPException(status_code=404, detail="artifact file not found")

        return FileResponse(path)

    @app.get(
        "/api/map/jobs/{job_id}/artifact/{artifact_name}",
        dependencies=[Depends(require_api_key)],
    )
    async def vector_map_job_artifact_alias(job_id: str, artifact_name: str, request: Request):
        return await vector_map_job_artifact(job_id, artifact_name, request)

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
