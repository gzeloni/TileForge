import json
import tempfile
import unittest
from pathlib import Path

from app.services.maps import SourceDefinition
from app.services.sync import ManifestSourceSync, ScheduledSourceSync, SourceSyncer


class FakeFetcher:
    def __init__(self, headers: dict[str, str], payload: bytes) -> None:
        self.headers = headers
        self.payload = payload
        self.head_calls = 0
        self.download_calls = 0

    def head(self, url: str) -> dict[str, str]:
        self.head_calls += 1
        return self.headers

    def download(self, url: str, destination: Path) -> None:
        self.download_calls += 1
        destination.write_bytes(self.payload)


class SourceSyncerTests(unittest.TestCase):
    def test_downloads_when_source_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = SourceDefinition(
                source_id="south-america",
                path=root / "south-america.osm.pbf",
                west=-92.0,
                south=-57.0,
                east=-27.0,
                north=15.0,
                download_url="https://example.test/south-america.osm.pbf",
            )
            fetcher = FakeFetcher(
                headers={"etag": "v1", "last-modified": "Mon, 20 Apr 2026 21:00:00 GMT"},
                payload=b"source-v1",
            )

            result = SourceSyncer(fetcher=fetcher).sync_source(source)

            self.assertEqual(result["status"], "downloaded")
            self.assertEqual(source.path.read_bytes(), b"source-v1")
            self.assertEqual(fetcher.download_calls, 1)

    def test_skips_download_when_remote_fingerprint_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = SourceDefinition(
                source_id="south-america",
                path=root / "south-america.osm.pbf",
                west=-92.0,
                south=-57.0,
                east=-27.0,
                north=15.0,
                download_url="https://example.test/south-america.osm.pbf",
            )
            source.path.write_bytes(b"source-v1")
            state_path = source.path.with_name(f"{source.path.name}.sync.json")
            state_path.write_text(
                json.dumps(
                    {
                        "etag": "v1",
                        "last_modified": "Mon, 20 Apr 2026 21:00:00 GMT",
                    }
                ),
                encoding="utf-8",
            )
            fetcher = FakeFetcher(
                headers={"etag": "v1", "last-modified": "Mon, 20 Apr 2026 21:00:00 GMT"},
                payload=b"source-v1-new",
            )

            result = SourceSyncer(fetcher=fetcher).sync_source(source)

            self.assertEqual(result["status"], "unchanged")
            self.assertEqual(source.path.read_bytes(), b"source-v1")
            self.assertEqual(fetcher.download_calls, 0)


class ManifestSourceSyncTests(unittest.TestCase):
    def test_sync_once_updates_manifest_signatures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest_path = root / "sources" / "manifest.json"
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            source_path = root / "south-america-latest.osm.pbf"
            manifest_path.write_text(
                json.dumps(
                    {
                        "sources": [
                            {
                                "source_id": "south-america-latest",
                                "path": str(source_path),
                                "bounds": {
                                    "west": -92.0,
                                    "south": -57.0,
                                    "east": -27.0,
                                    "north": 15.0,
                                },
                                "download_url": "https://example.test/south-america-latest.osm.pbf",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            fetcher = FakeFetcher(
                headers={"etag": "v2", "last-modified": "Tue, 21 Apr 2026 03:00:00 GMT"},
                payload=b"source-v2",
            )

            results = ManifestSourceSync(
                maps_root=root,
                manifest_path=manifest_path,
                syncer=SourceSyncer(fetcher=fetcher),
            ).sync_once()

            self.assertEqual(results[0]["status"], "downloaded")
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["sources"][0]["download_url"], "https://example.test/south-america-latest.osm.pbf")
            self.assertTrue(payload["sources"][0]["signature"])

    def test_scheduled_source_sync_runs_once_on_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest_path = root / "sources" / "manifest.json"
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            source_path = root / "south-america-latest.osm.pbf"
            manifest_path.write_text(
                json.dumps(
                    {
                        "sources": [
                            {
                                "source_id": "south-america-latest",
                                "path": str(source_path),
                                "bounds": {
                                    "west": -92.0,
                                    "south": -57.0,
                                    "east": -27.0,
                                    "north": 15.0,
                                },
                                "download_url": "https://example.test/south-america-latest.osm.pbf",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            fetcher = FakeFetcher(
                headers={"etag": "v1", "last-modified": "Tue, 21 Apr 2026 03:00:00 GMT"},
                payload=b"source-v1",
            )
            scheduler = ScheduledSourceSync(
                manifest_sync=ManifestSourceSync(
                    maps_root=root,
                    manifest_path=manifest_path,
                    syncer=SourceSyncer(fetcher=fetcher),
                ),
                interval_seconds=3600,
                run_on_start=True,
            )

            scheduler.start()
            try:
                for _ in range(50):
                    if source_path.exists():
                        break
                    scheduler._stop_event.wait(0.02)
            finally:
                scheduler.stop()

            self.assertTrue(source_path.exists())
            self.assertEqual(fetcher.download_calls, 1)


if __name__ == "__main__":
    unittest.main()
