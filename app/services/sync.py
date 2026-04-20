import json
from pathlib import Path

import httpx

from app.services.maps import SourceDefinition


class HttpSourceFetcher:
    def __init__(self, timeout: float = 120.0) -> None:
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
        self.fetcher = fetcher or HttpSourceFetcher()

    def sync_source(self, source: SourceDefinition) -> dict[str, str]:
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
