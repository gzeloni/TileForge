"""Job orchestration for map builds."""

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass

from app.helpers.model import MapArea, MapJobAcceptedResponse, MapJobStatusResponse
from app.services.derived import derived_source_manager
from app.services.maps import MapService, map_service


@dataclass
class JobSnapshot:
    """Persisted job status."""

    job_id: str
    status: str
    cache_key: str
    source_id: str
    error: str | None = None
    result: dict | None = None

    def to_response(self) -> MapJobStatusResponse:
        return MapJobStatusResponse(
            job_id=self.job_id,
            status=self.status,
            cache_key=self.cache_key,
            source_id=self.source_id,
            error=self.error,
            result=self.result,
        )


def _package_status(status: str) -> str:
    return {
        "queued": "queued",
        "running": "building",
        "succeeded": "ready",
        "failed": "failed",
    }.get(status, status)


def _package_payload_from_job(job: MapJobStatusResponse) -> dict:
    expires_at = None
    if job.result is not None:
        expires_at = (
            getattr(job.result, "expires_at", None)
            if hasattr(job.result, "expires_at")
            else job.result.get("expires_at")
        )
    return {
        "package_id": job.cache_key,
        "status": _package_status(job.status),
        "source_id": job.source_id,
        "job_id": job.job_id,
        "error": job.error,
        "expires_at": expires_at,
    }


def _package_payload_from_meta(
    package_id: str,
    package_dir,
    meta: dict,
    job_id: str | None = None,
) -> dict:
    return {
        "package_id": package_id,
        "status": "ready",
        "source_id": meta["source_id"],
        "job_id": job_id,
        "error": None,
        "expires_at": meta["expires_at"],
        "artifacts": {
            "region_pbf": str(package_dir / "region.osm.pbf"),
            "mbtiles": str(package_dir / "map.mbtiles"),
            "graph": str(package_dir / "graph.json"),
            "addresses": str(package_dir / "addresses.sqlite3"),
            "meta": str(package_dir / "meta.json"),
        },
    }


class PackageAwareJobServiceMixin:
    """Add package endpoints on top of a job service."""

    def submit_package(self, area: MapArea) -> dict:
        job = self.submit_job(area)
        return self.get_package(job.cache_key)

    def get_package(self, package_id: str) -> dict:
        package_dir = self.map_service.cache_store.find_package_by_cache_key(package_id)
        latest_job_id = self.state_store.get_latest_job_id(package_id)
        if package_dir is not None:
            meta = self.map_service.cache_store.read_meta(package_dir)
            return _package_payload_from_meta(package_id, package_dir, meta, latest_job_id)

        if latest_job_id is None:
            raise KeyError(package_id)

        return _package_payload_from_job(self.get_job(latest_job_id))

    def stream_package(self, package_id: str):
        latest_job_id = self.state_store.get_latest_job_id(package_id)
        if latest_job_id is not None:
            yield from self.stream_job(latest_job_id)
            return

        package_dir = self.map_service.cache_store.find_package_by_cache_key(package_id)
        if package_dir is None:
            raise KeyError(package_id)

        meta = self.map_service.cache_store.read_meta(package_dir)
        yield {"event": "result", "data": _package_payload_from_meta(package_id, package_dir, meta)}

    def get_package_artifact_path(self, package_id: str, artifact_name: str):
        package_dir = self.map_service.cache_store.find_package_by_cache_key(package_id)
        if package_dir is None:
            latest_job_id = self.state_store.get_latest_job_id(package_id)
            if latest_job_id is not None:
                raise RuntimeError("package is not ready")
            raise KeyError(package_id)

        meta = self.map_service.cache_store.read_meta(package_dir)
        artifact = meta.get("artifacts", {}).get(artifact_name)
        if artifact is None:
            raise KeyError(f"artifact:{artifact_name}")
        path = package_dir / artifact
        if not path.exists():
            raise FileNotFoundError(path)
        return path


class InMemoryJobStateStore:
    def __init__(self) -> None:
        """Create an in-memory state store."""

        self.snapshots: dict[str, JobSnapshot] = {}
        self.events: dict[str, list[dict]] = {}
        self.active_by_cache_key: dict[str, str] = {}
        self.latest_by_cache_key: dict[str, str] = {}
        self.lock = threading.Lock()

    def create_job(self, job_id: str, cache_key: str, source_id: str) -> JobSnapshot:
        snapshot = JobSnapshot(
            job_id=job_id,
            status="queued",
            cache_key=cache_key,
            source_id=source_id,
        )
        with self.lock:
            self.snapshots[job_id] = snapshot
            self.events[job_id] = [
                {
                    "event": "job",
                    "job_id": job_id,
                    "status": "queued",
                    "cache_key": cache_key,
                    "source_id": source_id,
                }
            ]
            self.active_by_cache_key[cache_key] = job_id
            self.latest_by_cache_key[cache_key] = job_id
        return snapshot

    def get_job(self, job_id: str) -> MapJobStatusResponse:
        with self.lock:
            snapshot = self.snapshots.get(job_id)
            if snapshot is None:
                raise KeyError(job_id)
            return snapshot.to_response()

    def get_active_job_id(self, cache_key: str) -> str | None:
        with self.lock:
            return self.active_by_cache_key.get(cache_key)

    def get_latest_job_id(self, cache_key: str) -> str | None:
        with self.lock:
            return self.latest_by_cache_key.get(cache_key)

    def set_status(self, job_id: str, status: str) -> None:
        with self.lock:
            self.snapshots[job_id].status = status
            self.events[job_id].append({"event": "job", "job_id": job_id, "status": status})

    def append_event(self, job_id: str, event: dict) -> None:
        with self.lock:
            self.events[job_id].append(event)

    def set_result(self, job_id: str, result: dict) -> None:
        with self.lock:
            self.snapshots[job_id].status = "succeeded"
            self.snapshots[job_id].result = result
            self.events[job_id].append({"event": "result", "data": result})

    def set_error(self, job_id: str, message: str) -> None:
        with self.lock:
            self.snapshots[job_id].status = "failed"
            self.snapshots[job_id].error = message
            self.events[job_id].append({"event": "error", "detail": message, "status_code": 500})

    def clear_active(self, cache_key: str, job_id: str) -> None:
        with self.lock:
            if self.active_by_cache_key.get(cache_key) == job_id:
                del self.active_by_cache_key[cache_key]

    def stream(self, job_id: str):
        index = 0
        while True:
            with self.lock:
                if job_id not in self.events:
                    raise KeyError(job_id)
                events = list(self.events[job_id])
                status = self.snapshots[job_id].status
            while index < len(events):
                yield events[index]
                index += 1
            if status in {"succeeded", "failed"}:
                break
            time.sleep(0.01)


class InlineMapJobService(PackageAwareJobServiceMixin):
    def __init__(self, map_service: MapService) -> None:
        """Create an inline job service."""

        self.map_service = map_service
        self.state_store = InMemoryJobStateStore()

    def submit_job(self, area: MapArea) -> MapJobAcceptedResponse:
        source, effective_area, cache_key, package_dir = self.map_service.prepare(area)
        self.map_service.track_usage(area, source)
        active_job_id = self.state_store.get_active_job_id(cache_key)
        if active_job_id:
            existing = self.state_store.get_job(active_job_id)
            return MapJobAcceptedResponse(
                job_id=existing.job_id,
                status=existing.status,
                cache_key=existing.cache_key,
                source_id=existing.source_id,
            )

        job_id = str(uuid.uuid4())
        snapshot = self.state_store.create_job(job_id, cache_key, source.source_id)

        def worker() -> None:
            self.state_store.set_status(job_id, "running")

            def reporter(step: str, message: str) -> None:
                self.state_store.append_event(
                    job_id,
                    {"event": "progress", "step": step, "message": message},
                )

            try:
                result = self.map_service.build_prepared(
                    source=source,
                    area=effective_area,
                    cache_key=cache_key,
                    package_dir=package_dir,
                    reporter=reporter,
                )
                self.state_store.set_result(job_id, result.model_dump(mode="json"))
            except Exception as exc:
                self.state_store.set_error(job_id, str(exc))
            finally:
                self.state_store.clear_active(cache_key, job_id)

        threading.Thread(target=worker, daemon=True).start()
        return MapJobAcceptedResponse(
            job_id=snapshot.job_id,
            status=snapshot.status,
            cache_key=snapshot.cache_key,
            source_id=snapshot.source_id,
        )

    def get_job(self, job_id: str) -> MapJobStatusResponse:
        return self.state_store.get_job(job_id)

    def stream_job(self, job_id: str):
        yield from self.state_store.stream(job_id)

    def submit_and_stream(self, area: MapArea):
        job = self.submit_job(area)
        yield from self.stream_job(job.job_id)


class RedisJobStateStore:
    def __init__(self, redis_url: str, ttl_seconds: int = 43200, namespace: str = "homelander") -> None:
        """Create a Redis-backed state store."""

        from redis import Redis

        self.redis = Redis.from_url(redis_url, decode_responses=True)
        self.ttl_seconds = ttl_seconds
        self.namespace = namespace

    def create_job(self, job_id: str, cache_key: str, source_id: str) -> JobSnapshot:
        snapshot = JobSnapshot(
            job_id=job_id,
            status="queued",
            cache_key=cache_key,
            source_id=source_id,
        )
        pipeline = self.redis.pipeline()
        pipeline.set(self._snapshot_key(job_id), json.dumps(snapshot.__dict__), ex=self.ttl_seconds)
        pipeline.rpush(
            self._events_key(job_id),
            json.dumps(
                {
                    "event": "job",
                    "job_id": job_id,
                    "status": "queued",
                    "cache_key": cache_key,
                    "source_id": source_id,
                }
            ),
        )
        pipeline.expire(self._events_key(job_id), self.ttl_seconds)
        pipeline.set(self._active_key(cache_key), job_id, ex=self.ttl_seconds)
        pipeline.set(self._latest_key(cache_key), job_id, ex=self.ttl_seconds)
        pipeline.execute()
        return snapshot

    def get_job(self, job_id: str) -> MapJobStatusResponse:
        payload = self.redis.get(self._snapshot_key(job_id))
        if payload is None:
            raise KeyError(job_id)
        snapshot = JobSnapshot(**json.loads(payload))
        return snapshot.to_response()

    def get_active_job_id(self, cache_key: str) -> str | None:
        return self.redis.get(self._active_key(cache_key))

    def get_latest_job_id(self, cache_key: str) -> str | None:
        return self.redis.get(self._latest_key(cache_key))

    def set_status(self, job_id: str, status: str) -> None:
        snapshot = self._snapshot(job_id)
        snapshot.status = status
        self._store_snapshot(snapshot)
        self.append_event(job_id, {"event": "job", "job_id": job_id, "status": status})

    def append_event(self, job_id: str, event: dict) -> None:
        self.redis.rpush(self._events_key(job_id), json.dumps(event))
        self.redis.expire(self._events_key(job_id), self.ttl_seconds)

    def set_result(self, job_id: str, result: dict) -> None:
        snapshot = self._snapshot(job_id)
        snapshot.status = "succeeded"
        snapshot.result = result
        self._store_snapshot(snapshot)
        self.append_event(job_id, {"event": "result", "data": result})

    def set_error(self, job_id: str, message: str) -> None:
        snapshot = self._snapshot(job_id)
        snapshot.status = "failed"
        snapshot.error = message
        self._store_snapshot(snapshot)
        self.append_event(job_id, {"event": "error", "detail": message, "status_code": 500})

    def clear_active(self, cache_key: str, job_id: str) -> None:
        active_key = self._active_key(cache_key)
        if self.redis.get(active_key) == job_id:
            self.redis.delete(active_key)

    def stream(self, job_id: str):
        index = 0
        while True:
            events = self.redis.lrange(self._events_key(job_id), index, -1)
            if not events and self.redis.get(self._snapshot_key(job_id)) is None:
                raise KeyError(job_id)
            for raw_event in events:
                index += 1
                yield json.loads(raw_event)
            snapshot = self.get_job(job_id)
            if snapshot.status in {"succeeded", "failed"}:
                break
            time.sleep(0.5)

    def _snapshot(self, job_id: str) -> JobSnapshot:
        payload = self.redis.get(self._snapshot_key(job_id))
        if payload is None:
            raise KeyError(job_id)
        return JobSnapshot(**json.loads(payload))

    def _store_snapshot(self, snapshot: JobSnapshot) -> None:
        self.redis.set(
            self._snapshot_key(snapshot.job_id),
            json.dumps(snapshot.__dict__),
            ex=self.ttl_seconds,
        )

    def _snapshot_key(self, job_id: str) -> str:
        return f"{self.namespace}:job:{job_id}:snapshot"

    def _events_key(self, job_id: str) -> str:
        return f"{self.namespace}:job:{job_id}:events"

    def _active_key(self, cache_key: str) -> str:
        return f"{self.namespace}:cache:{cache_key}:active"

    def _latest_key(self, cache_key: str) -> str:
        return f"{self.namespace}:cache:{cache_key}:latest"


class RQMapJobService(PackageAwareJobServiceMixin):
    def __init__(self, map_service: MapService, queue, state_store) -> None:
        """Create an RQ-backed job service."""

        self.map_service = map_service
        self.queue = queue
        self.state_store = state_store

    def submit_job(self, area: MapArea) -> MapJobAcceptedResponse:
        source, effective_area, cache_key, package_dir = self.map_service.prepare(area)
        self.map_service.track_usage(area, source)

        if self.map_service.cache_store.is_ready(package_dir):
            job_id = f"cache-{cache_key}"
            try:
                status = self.state_store.get_job(job_id)
            except KeyError:
                self.state_store.create_job(job_id, cache_key, source.source_id)
                self.state_store.set_result(
                    job_id,
                    self.map_service._response_for(
                        cache_key=cache_key,
                        source=source,
                        package_dir=package_dir,
                        cached=True,
                    ).model_dump(mode="json"),
                )
                status = self.state_store.get_job(job_id)
            return MapJobAcceptedResponse(
                job_id=status.job_id,
                status=status.status,
                cache_key=status.cache_key,
                source_id=status.source_id,
            )

        active_job_id = self.state_store.get_active_job_id(cache_key)
        if active_job_id:
            existing = self.state_store.get_job(active_job_id)
            return MapJobAcceptedResponse(
                job_id=existing.job_id,
                status=existing.status,
                cache_key=existing.cache_key,
                source_id=existing.source_id,
            )

        job_id = str(uuid.uuid4())
        snapshot = self.state_store.create_job(job_id, cache_key, source.source_id)
        try:
            self.queue.enqueue(
                "app.services.jobs.run_job",
                job_id,
                area.model_dump(mode="json"),
                cache_key,
                source.source_id,
                job_id=job_id,
                result_ttl=self.state_store.ttl_seconds,
                failure_ttl=self.state_store.ttl_seconds,
            )
        except Exception:
            self.state_store.clear_active(cache_key, job_id)
            raise
        return MapJobAcceptedResponse(
            job_id=snapshot.job_id,
            status=snapshot.status,
            cache_key=snapshot.cache_key,
            source_id=snapshot.source_id,
        )

    def get_job(self, job_id: str) -> MapJobStatusResponse:
        return self.state_store.get_job(job_id)

    def stream_job(self, job_id: str):
        yield from self.state_store.stream(job_id)

    def submit_and_stream(self, area: MapArea):
        job = self.submit_job(area)
        yield from self.stream_job(job.job_id)


def run_job(job_id: str, area_payload: dict, cache_key: str, source_id: str) -> dict:
    """Execute a queued map job."""

    redis_url = os.getenv("REDIS_URL", "redis://redis:6379/0")
    state_store = RedisJobStateStore(
        redis_url=redis_url,
        ttl_seconds=int(os.getenv("JOB_TTL_SECONDS", "43200")),
    )
    area = MapArea(**area_payload)
    maps = map_service()
    maps.usage_tracker = derived_source_manager()
    state_store.set_status(job_id, "running")

    def reporter(step: str, message: str) -> None:
        state_store.append_event(
            job_id,
            {"event": "progress", "step": step, "message": message},
        )

    try:
        source, effective_area, prepared_cache_key, package_dir = maps.prepare(area)
        result = maps.build_prepared(
            source=source,
            area=effective_area,
            cache_key=prepared_cache_key,
            package_dir=package_dir,
            reporter=reporter,
        )
        payload = result.model_dump(mode="json")
        state_store.set_result(job_id, payload)
        return payload
    except Exception as exc:
        state_store.set_error(job_id, str(exc))
        raise
    finally:
        state_store.clear_active(cache_key, job_id)


def job_service():
    """Build the default job service from environment settings."""

    from rq import Queue

    redis_url = os.getenv("REDIS_URL", "redis://redis:6379/0")
    queue_name = os.getenv("RQ_QUEUE_NAME", "homelander-maps")
    ttl_seconds = int(os.getenv("JOB_TTL_SECONDS", "43200"))

    state_store = RedisJobStateStore(redis_url=redis_url, ttl_seconds=ttl_seconds)
    queue = Queue(name=queue_name, connection=state_store.redis, default_timeout="1h")
    maps = map_service()
    maps.usage_tracker = derived_source_manager()
    return RQMapJobService(
        map_service=maps,
        queue=queue,
        state_store=state_store,
    )
