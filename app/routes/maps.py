"""Map routes."""

import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse, StreamingResponse

from app.helpers.model import (
    ArtifactPaths,
    MapArea,
    MapJobAcceptedResponse,
    MapJobStatusResponse,
    MapPackageAcceptedResponse,
    MapPackageStatusResponse,
)
from app.routes.deps import jobs, require_key

router = APIRouter(prefix="/api/map", tags=["map"], dependencies=[Depends(require_key)])


def links(package_id: str) -> ArtifactPaths:
    """Build package artifact links."""

    return ArtifactPaths(
        region_pbf=f"/api/map/packages/{package_id}/artifacts/region_pbf",
        mbtiles=f"/api/map/packages/{package_id}/artifacts/mbtiles",
        graph=f"/api/map/packages/{package_id}/artifacts/graph",
        meta=f"/api/map/packages/{package_id}/artifacts/meta",
    )


def accepted(payload: dict) -> MapPackageAcceptedResponse:
    """Convert a package payload into an accepted response."""

    package_id = payload["package_id"]
    return MapPackageAcceptedResponse(
        package_id=package_id,
        status=payload["status"],
        source_id=payload["source_id"],
        job_id=payload.get("job_id"),
        expires_at=payload.get("expires_at"),
        artifacts=links(package_id),
    )


def package(payload: dict) -> MapPackageStatusResponse:
    """Convert a package payload into a status response."""

    package_id = payload["package_id"]
    return MapPackageStatusResponse(
        package_id=package_id,
        status=payload["status"],
        source_id=payload["source_id"],
        job_id=payload.get("job_id"),
        error=payload.get("error"),
        expires_at=payload.get("expires_at"),
        artifacts=links(package_id),
    )


@router.post("", response_model=MapJobAcceptedResponse, status_code=status.HTTP_202_ACCEPTED)
async def post_map(area: MapArea, service=Depends(jobs)):
    """Submit a map job."""

    try:
        return service.submit_job(area)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/packages", response_model=MapPackageAcceptedResponse, status_code=status.HTTP_202_ACCEPTED)
async def post_package(area: MapArea, service=Depends(jobs)):
    """Submit or reuse a package build."""

    try:
        return accepted(service.submit_package(area))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/packages/{package_id}", response_model=MapPackageStatusResponse)
async def get_package(package_id: str, service=Depends(jobs)):
    """Return the current package status."""

    try:
        return package(service.get_package(package_id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="package not found") from exc


@router.get("/packages/{package_id}/artifacts/{artifact_name}")
async def get_package_artifact(package_id: str, artifact_name: str, service=Depends(jobs)):
    """Download a package artifact."""

    try:
        path = service.get_package_artifact_path(package_id, artifact_name)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except KeyError as exc:
        detail = "artifact not found" if str(exc).strip("'") == f"artifact:{artifact_name}" else "package not found"
        raise HTTPException(status_code=404, detail=detail) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="artifact file not found") from exc
    return FileResponse(path)


@router.get("/packages/{package_id}/stream")
async def stream_package(package_id: str, service=Depends(jobs)):
    """Stream package progress as NDJSON."""

    def events():
        try:
            for event in service.stream_package(package_id):
                yield json.dumps(event) + "\n"
        except KeyError:
            yield json.dumps({"event": "error", "detail": "package not found", "status_code": 404}) + "\n"

    return StreamingResponse(events(), media_type="application/x-ndjson")


@router.get("/jobs/{job_id}", response_model=MapJobStatusResponse)
async def get_job(job_id: str, service=Depends(jobs)):
    """Return the current job status."""

    try:
        return service.get_job(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="job not found") from exc


@router.post("/stream")
async def post_stream(area: MapArea, service=Depends(jobs)):
    """Submit a job and stream progress as NDJSON."""

    def events():
        for event in service.submit_and_stream(area):
            yield json.dumps(event) + "\n"

    return StreamingResponse(events(), media_type="application/x-ndjson")


@router.get("/jobs/{job_id}/artifacts/{artifact_name}")
async def get_job_artifact(job_id: str, artifact_name: str, service=Depends(jobs)):
    """Download a job artifact."""

    job = service.get_job(job_id)
    if job.result is None:
        raise HTTPException(status_code=409, detail="job result is not available")

    artifact = {
        "mbtiles": job.result.artifacts.mbtiles,
        "graph": job.result.artifacts.graph,
        "meta": job.result.artifacts.meta,
        "region_pbf": job.result.artifacts.region_pbf,
    }.get(artifact_name)
    if artifact is None:
        raise HTTPException(status_code=404, detail="artifact not found")

    path = Path(artifact)
    if not path.exists():
        raise HTTPException(status_code=404, detail="artifact file not found")
    return FileResponse(path)


@router.get("/jobs/{job_id}/artifact/{artifact_name}")
async def get_job_artifact_alias(job_id: str, artifact_name: str, service=Depends(jobs)):
    """Keep compatibility with the legacy singular artifact path."""

    return await get_job_artifact(job_id, artifact_name, service)


@router.get("/jobs/{job_id}/stream")
async def stream_job(job_id: str, service=Depends(jobs)):
    """Stream job progress as NDJSON."""

    def events():
        try:
            for event in service.stream_job(job_id):
                yield json.dumps(event) + "\n"
        except KeyError:
            yield json.dumps({"event": "error", "detail": "job not found", "status_code": 404}) + "\n"

    return StreamingResponse(events(), media_type="application/x-ndjson")
