import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from app.helpers.model import MapArea
from app.services.derived import (
    DerivedRegionDefinition,
    DerivedRegionCatalog,
    DerivedSourceIndex,
    DerivedSourceManager,
    RegionBoundsResolver,
    derived_source_manager,
)
from app.services.maps import SourceDefinition


class FakeExtractor:
    def __init__(self) -> None:
        self.calls: list[tuple[Path, Path]] = []

    def extract(self, source_path: Path, area: MapArea, output_path: Path) -> None:
        self.calls.append((source_path, output_path))
        output_path.write_bytes(b"derived")


class DerivedSourceManagerTests(unittest.TestCase):
    def test_env_default_region_creates_derived_source_and_manifest_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest_path = root / "sources" / "manifest.json"
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            source_path = root / "south-america.osm.pbf"
            source_path.write_bytes(b"parent")
            manifest_path.write_text(
                json.dumps(
                    {
                        "sources": [
                            {
                                "source_id": "south-america",
                                "path": str(source_path),
                                "bounds": {
                                    "west": -92.0,
                                    "south": -57.0,
                                    "east": -27.0,
                                    "north": 15.0,
                                },
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            env = {
                "MAPS_ROOT": str(root),
                "SOURCES_MANIFEST": str(manifest_path),
                "DERIVED_INDEX_PATH": str(root / "cache" / "derived.sqlite3"),
                "DERIVED_DEFAULT_REGIONS": json.dumps(
                    [
                        {
                            "region_id": "brazil",
                            "source_id": "brazil",
                            "parent_source_id": "south-america",
                            "path": "derived/brazil.osm.pbf",
                            "query": "Brazil",
                            "country_codes": "br",
                        }
                    ]
                ),
                "DERIVED_ADAPTIVE_REGIONS": "[]",
            }

            with patch.dict("os.environ", env, clear=False), patch.object(
                RegionBoundsResolver,
                "_fetch_bounds",
                return_value={
                    "west": -74.0,
                    "south": -34.0,
                    "east": -34.0,
                    "north": 6.0,
                },
            ):
                manager = derived_source_manager()
                assert manager is not None
                fake_extractor = FakeExtractor()
                manager.extractor = fake_extractor

                created = manager.ensure_defaults()

                self.assertEqual(created, ["brazil"])
                self.assertEqual(len(fake_extractor.calls), 1)
                payload = json.loads(manifest_path.read_text(encoding="utf-8"))
                self.assertEqual(payload["sources"][-1]["source_id"], "south-america")
                derived_entry = next(
                    item for item in payload["sources"] if item["source_id"] == "brazil"
                )
                self.assertEqual(derived_entry["kind"], "default")
                self.assertEqual(derived_entry["parent_source_id"], "south-america")
                self.assertTrue((root / "derived" / "brazil.osm.pbf").exists())

    def test_region_bounds_resolver_uses_cached_result_for_one_week(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            resolver = RegionBoundsResolver(
                cache_path=root / "cache" / "derived.sqlite3",
                ttl=timedelta(days=7),
                base_url="https://nominatim.openstreetmap.org/search",
                user_agent="test-agent",
            )
            item = {
                "region_id": "brazil",
                "source_id": "brazil",
                "query": "Brazil",
                "country_codes": "br",
            }

            with patch.object(
                resolver,
                "_fetch_bounds",
                return_value={
                    "west": -74.0,
                    "south": -34.0,
                    "east": -34.0,
                    "north": 6.0,
                },
            ) as fetch:
                first = resolver.resolve(item)
                second = resolver.resolve(item)

            self.assertEqual(first, second)
            self.assertEqual(fetch.call_count, 1)

    def test_region_bounds_resolver_refreshes_cache_after_ttl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            resolver = RegionBoundsResolver(
                cache_path=root / "cache" / "derived.sqlite3",
                ttl=timedelta(days=7),
                base_url="https://nominatim.openstreetmap.org/search",
                user_agent="test-agent",
            )
            item = {
                "region_id": "brazil",
                "source_id": "brazil",
                "query": "Brazil",
                "country_codes": "br",
            }

            with patch.object(
                resolver,
                "_fetch_bounds",
                side_effect=[
                    {
                        "west": -74.0,
                        "south": -34.0,
                        "east": -34.0,
                        "north": 6.0,
                    },
                    {
                        "west": -73.9,
                        "south": -34.1,
                        "east": -34.0,
                        "north": 6.1,
                    },
                ],
            ) as fetch:
                first = resolver.resolve(item)
                with resolver._connect() as connection:
                    connection.execute(
                        """
                        UPDATE region_bounds_cache
                        SET fetched_at = ?
                        """,
                        ((datetime.now(UTC) - timedelta(days=8)).isoformat(),),
                    )
                second = resolver.resolve(item)

            self.assertNotEqual(first, second)
            self.assertEqual(fetch.call_count, 2)

    def test_request_count_promotes_adaptive_region_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            index = DerivedSourceIndex(root / "cache" / "derived.sqlite3")
            catalog = DerivedRegionCatalog(
                [
                    DerivedRegionDefinition(
                        region_id="parana",
                        source_id="br-parana",
                        parent_source_id="brazil",
                        west=-55.0,
                        south=-27.0,
                        east=-47.0,
                        north=-22.0,
                        path=root / "derived" / "br-parana.osm.pbf",
                        kind="adaptive",
                        request_threshold=2,
                        pinned=False,
                    )
                ]
            )
            manager = DerivedSourceManager(
                maps_root=root,
                manifest_path=root / "sources" / "manifest.json",
                index=index,
                catalog=catalog,
                extractor=FakeExtractor(),
                idle_ttl=timedelta(hours=24),
            )
            source = SourceDefinition(
                source_id="south-america",
                path=root / "south-america.osm.pbf",
                west=-92.0,
                south=-57.0,
                east=-27.0,
                north=15.0,
            )
            source.path.write_bytes(b"parent")

            area = MapArea(west=-49.5, south=-25.7, east=-49.0, north=-25.2)
            manager.record_request(area, source)
            manager.record_request(area, source)

            promotable = index.list_promotable_regions(catalog)

            self.assertEqual([region.region_id for region in promotable], ["parana"])

    def test_cleanup_stale_source_removes_manifest_entry_and_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest_path = root / "sources" / "manifest.json"
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            source_path = root / "south-america.osm.pbf"
            derived_path = root / "derived" / "br-parana.osm.pbf"
            source_path.write_bytes(b"parent")
            derived_path.parent.mkdir(parents=True, exist_ok=True)
            derived_path.write_bytes(b"derived")
            manifest_path.write_text(
                json.dumps(
                    {
                        "sources": [
                            {
                                "source_id": "south-america",
                                "path": str(source_path),
                                "bounds": {
                                    "west": -92.0,
                                    "south": -57.0,
                                    "east": -27.0,
                                    "north": 15.0,
                                },
                            },
                            {
                                "source_id": "br-parana",
                                "path": str(derived_path),
                                "bounds": {
                                    "west": -55.0,
                                    "south": -27.0,
                                    "east": -47.0,
                                    "north": -22.0,
                                },
                                "kind": "adaptive",
                                "region_id": "parana",
                                "parent_source_id": "brazil",
                                "parent_source_signature": "sig",
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            catalog = DerivedRegionCatalog(
                [
                    DerivedRegionDefinition(
                        region_id="parana",
                        source_id="br-parana",
                        parent_source_id="brazil",
                        west=-55.0,
                        south=-27.0,
                        east=-47.0,
                        north=-22.0,
                        path=derived_path,
                        kind="adaptive",
                        request_threshold=2,
                    )
                ]
            )
            index = DerivedSourceIndex(root / "cache" / "derived.sqlite3")
            manager = DerivedSourceManager(
                maps_root=root,
                manifest_path=manifest_path,
                index=index,
                catalog=catalog,
                extractor=FakeExtractor(),
                idle_ttl=timedelta(hours=1),
            )
            index.register_derived_source(catalog.regions[0], "sig")
            with index._connect() as connection:
                connection.execute(
                    """
                    UPDATE derived_sources
                    SET last_used_at = ?, status = 'active'
                    WHERE source_id = ?
                    """,
                    (
                        (datetime.now(UTC) - timedelta(hours=2)).isoformat(),
                        "br-parana",
                    ),
                )

            deleted = manager.cleanup_stale_sources()

            self.assertEqual(deleted, ["br-parana"])
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual([item["source_id"] for item in payload["sources"]], ["south-america"])
            self.assertFalse(derived_path.exists())


if __name__ == "__main__":
    unittest.main()
