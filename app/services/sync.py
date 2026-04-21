"""Source synchronization services."""

import json
import os
import threading
import time
import fcntl
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import httpx

from app.services.maps import (
    OsmiumSourceInspector,
    SourceDefinition,
    SourceRegistry,
    ensure_sources_manifest,
)


class HttpSourceFetcher:
    def __init__(self, timeout: float = 120.0) -> None:
        """Create an HTTP fetcher for remote source files."""

        self.timeout = timeout

    def head(self, url: str) -> dict[str, str]:
        response = httpx.head(url, follow_redirects=True, timeout=self.timeout)
        response.raise_for_status()
        return dict(response.headers)

    def download(self, url: str, destination: Path) -> None:
        with httpx.stream(
            "GET",
            url,
            follow_redirects=True,
            timeout=self.timeout,
        ) as response:
            response.raise_for_status()
            with destination.open("wb") as handle:
                for chunk in response.iter_bytes():
                    handle.write(chunk)


class SourceSyncer:
    def __init__(self, fetcher=None) -> None:
        """Create a source synchronizer."""

        self.fetcher = fetcher or HttpSourceFetcher()

    def sync_source(self, source: SourceDefinition) -> dict[str, str]:
        """Download a source when the remote fingerprint changes."""

        if not source.download_url:
            return {"source_id": source.source_id, "status": "skipped"}

        remote_state = self._remote_state(source.download_url)
        local_state = self._read_state(source)

        if source.path.exists() and local_state == remote_state:
            return {"source_id": source.source_id, "status": "unchanged"}

        source.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = source.path.with_name(f".{source.path.name}.download")
        self.fetcher.download(source.download_url, temp_path)
        temp_path.replace(source.path)
        self._write_state(source, remote_state)
        return {"source_id": source.source_id, "status": "downloaded"}

    def _remote_state(self, url: str) -> dict[str, str]:
        headers = self.fetcher.head(url)
        return {
            "etag": headers.get("etag", ""),
            "last_modified": headers.get("last-modified", ""),
        }

    def _state_path(self, source: SourceDefinition) -> Path:
        return source.path.with_name(f"{source.path.name}.sync.json")

    def _read_state(self, source: SourceDefinition) -> dict[str, str]:
        state_path = self._state_path(source)
        if not state_path.exists():
            return {}
        return json.loads(state_path.read_text(encoding="utf-8"))

    def _write_state(self, source: SourceDefinition, state: dict[str, str]) -> None:
        self._state_path(source).write_text(json.dumps(state), encoding="utf-8")


class ManifestSourceSync:
    def __init__(
        self,
        maps_root: Path,
        manifest_path: Path,
        syncer=None,
        inspector=None,
    ) -> None:
        """Create a manifest synchronizer."""

        self.maps_root = maps_root
        self.manifest_path = manifest_path
        self.syncer = syncer or SourceSyncer()
        self.inspector = inspector or OsmiumSourceInspector(binary=os.getenv("OSMIUM_BINARY", "osmium"))

    def sync_once(self) -> list[dict[str, str]]:
        """Synchronize all sources declared in the manifest once."""

        with self._lock():
            if self.manifest_path.exists():
                registry = SourceRegistry.from_file(self.manifest_path)
            else:
                registry = ensure_sources_manifest(
                    maps_root=self.maps_root,
                    manifest_path=self.manifest_path,
                    inspector=self.inspector,
                )
            results = [self.syncer.sync_source(source) for source in registry.sources]
            refreshed_registry = SourceRegistry(
                [self._refresh_source_definition(source) for source in registry.sources]
            )
            payload = refreshed_registry.to_manifest_payload()
            self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
            self.manifest_path.write_text(json.dumps(payload), encoding="utf-8")
            return results

    def _refresh_source_definition(self, source: SourceDefinition) -> SourceDefinition:
        if not source.path.exists():
            return source
        return replace(source, signature_override=source.signature())

    @contextmanager
    def _lock(self):
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.manifest_path.with_name(f"{self.manifest_path.name}.sync.lock")
        with lock_path.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class ScheduledSourceSync:
    def __init__(
        self,
        manifest_sync: ManifestSourceSync,
        interval_seconds: float = 86400.0,
        run_on_start: bool = True,
    ) -> None:
        """Create a background scheduler for source updates."""

        self.manifest_sync = manifest_sync
        self.interval_seconds = interval_seconds
        self.run_on_start = run_on_start
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        """Start the background synchronization loop."""

        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the background synchronization loop."""

        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _run(self) -> None:
        next_delay = 0.0 if self.run_on_start else self.interval_seconds
        while not self._stop_event.wait(next_delay):
            try:
                self.manifest_sync.sync_once()
            except Exception as exc:
                print(f"source sync failed: {exc}", flush=True)
            next_delay = self.interval_seconds


def source_sync() -> ScheduledSourceSync | None:
    """Build the default source synchronizer from environment settings."""

    enabled = os.getenv("ENABLE_SOURCE_SYNC", "1").lower() not in {"0", "false", "no"}
    if not enabled:
        return None

    maps_root = Path(os.getenv("MAPS_ROOT", "/osm_maps"))
    manifest_path = Path(
        os.getenv("SOURCES_MANIFEST", str(maps_root / "sources" / "manifest.json"))
    )
    interval_hours = float(os.getenv("SOURCE_SYNC_INTERVAL_HOURS", "24"))
    run_on_start = os.getenv("SOURCE_SYNC_ON_STARTUP", "1").lower() not in {"0", "false", "no"}
    timeout = float(os.getenv("SOURCE_SYNC_TIMEOUT_SECONDS", "120"))
    return ScheduledSourceSync(
        manifest_sync=ManifestSourceSync(
            maps_root=maps_root,
            manifest_path=manifest_path,
            syncer=SourceSyncer(fetcher=HttpSourceFetcher(timeout=timeout)),
            inspector=OsmiumSourceInspector(binary=os.getenv("OSMIUM_BINARY", "osmium")),
        ),
        interval_seconds=max(60.0, interval_hours * 3600),
        run_on_start=run_on_start,
    )
