import json
import tempfile
import unittest
from pathlib import Path

from app.services.maps import SourceDefinition
from app.services.sync import SourceSyncer


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


if __name__ == "__main__":
    unittest.main()
