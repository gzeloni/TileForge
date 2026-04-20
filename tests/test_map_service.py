import json
import os
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.api import create_app
from app.helpers.model import MapArea
from app.services.jobs import InlineMapJobService
from app.services.maps import (
    CacheStore,
    CompositeRegionPackageBuilder,
    MapService,
    SourceRegistry,
    build_default_service,
    ensure_sources_manifest,
)


class FakeRegionPackageBuilder:
    def __init__(self) -> None:
        self.calls = 0

    def build(self, source, area, output_dir: Path, reporter=None) -> None:
        self.calls += 1
        if reporter:
            reporter("extract", "Starting regional extract")
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "region.osm.pbf").write_bytes(b"pbf")
        if reporter:
            reporter("graph", "Building graph")
        (output_dir / "map.mbtiles").write_bytes(b"mbtiles")
        (output_dir / "graph.json").write_text(
            json.dumps({"road_types": ["residential"], "nodes": [], "edges": []}),
            encoding="utf-8",
        )
        if reporter:
            reporter("mbtiles", "Writing MBTiles")


class SourceRegistryTests(unittest.TestCase):
    def test_chooses_smallest_matching_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest_path = root / "sources.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "sources": [
                            {
                                "source_id": "south-america",
                                "path": str(root / "south.osm.pbf"),
                                "bounds": {
                                    "west": -92.0,
                                    "south": -57.0,
                                    "east": -27.0,
                                    "north": 15.0,
                                },
                            },
                            {
                                "source_id": "parana",
                                "path": str(root / "parana.osm.pbf"),
                                "bounds": {
                                    "west": -55.0,
                                    "south": -27.0,
                                    "east": -47.0,
                                    "north": -22.0,
                                },
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            (root / "south.osm.pbf").write_bytes(b"south")
            (root / "parana.osm.pbf").write_bytes(b"parana")

            registry = SourceRegistry.from_file(manifest_path)
            source = registry.resolve(
                MapArea(west=-49.5, south=-25.7, east=-49.0, north=-25.2)
            )

            self.assertEqual(source.source_id, "parana")

    def test_rejects_area_outside_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest_path = root / "sources.json"
            manifest_path.write_text(
                json.dumps(
                    [
                        {
                            "source_id": "central-america",
                            "path": str(root / "central.osm.pbf"),
                            "west": -118.0,
                            "south": 5.0,
                            "east": -74.0,
                            "north": 33.0,
                        }
                    ]
                ),
                encoding="utf-8",
            )
            (root / "central.osm.pbf").write_bytes(b"central")

            registry = SourceRegistry.from_file(manifest_path)

            with self.assertRaisesRegex(ValueError, "No source covers"):
                registry.resolve(
                    MapArea(west=-49.5, south=-25.7, east=-49.0, north=-25.2)
                )


class CacheStoreTests(unittest.TestCase):
    def test_cleanup_removes_expired_package(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = CacheStore(root / "cache", ttl=timedelta(hours=12))
            package_dir = root / "cache" / "south-america" / "abc"
            package_dir.mkdir(parents=True)
            (package_dir / "region.osm.pbf").write_bytes(b"pbf")
            (package_dir / "map.mbtiles").write_bytes(b"mbtiles")
            (package_dir / "graph.json").write_text("{}", encoding="utf-8")
            (package_dir / "meta.json").write_text(
                json.dumps(
                    {
                        "created_at": (
                            datetime.now(UTC) - timedelta(hours=13)
                        ).isoformat(),
                        "artifacts": {
                            "region_pbf": "region.osm.pbf",
                            "mbtiles": "map.mbtiles",
                            "graph": "graph.json",
                        },
                    }
                ),
                encoding="utf-8",
            )

            removed = store.cleanup()

            self.assertEqual(removed, 1)
            self.assertFalse(package_dir.exists())


class MapApiTests(unittest.TestCase):
    def test_post_submits_background_job_and_exposes_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest_path = root / "sources.json"
            source_path = root / "south-america.osm.pbf"
            source_path.write_bytes(b"south")
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

            registry = SourceRegistry.from_file(manifest_path)
            cache_store = CacheStore(root / "cache", ttl=timedelta(hours=12))
            builder = FakeRegionPackageBuilder()
            map_service = MapService(
                source_registry=registry,
                cache_store=cache_store,
                package_builder=builder,
                pipeline_version="test-v1",
            )
            service = InlineMapJobService(map_service=map_service)
            client = TestClient(create_app(service=service))
            payload = {
                "west": -49.5,
                "south": -25.7,
                "east": -49.0,
                "north": -25.2,
            }

            submitted = client.post("/api/map", json=payload)
            job_id = submitted.json()["job_id"]
            status = None
            for _ in range(10):
                status = client.get(f"/api/map/jobs/{job_id}")
                if status.json()["status"] == "succeeded":
                    break

            self.assertEqual(submitted.status_code, 202)
            self.assertEqual(status.status_code, 200)
            self.assertEqual(status.json()["status"], "succeeded")
            self.assertEqual(builder.calls, 1)
            self.assertTrue(Path(status.json()["result"]["artifacts"]["mbtiles"]).exists())

    def test_stream_endpoint_emits_progress_and_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest_path = root / "sources.json"
            source_path = root / "south-america.osm.pbf"
            source_path.write_bytes(b"south")
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

            registry = SourceRegistry.from_file(manifest_path)
            cache_store = CacheStore(root / "cache", ttl=timedelta(hours=12))
            builder = FakeRegionPackageBuilder()
            map_service = MapService(
                source_registry=registry,
                cache_store=cache_store,
                package_builder=builder,
                pipeline_version="test-v1",
            )
            service = InlineMapJobService(map_service=map_service)
            client = TestClient(create_app(service=service))
            payload = {
                "west": -49.5,
                "south": -25.7,
                "east": -49.0,
                "north": -25.2,
            }

            with client.stream("POST", "/api/map/stream", json=payload) as response:
                events = [json.loads(line) for line in response.iter_lines() if line]

            self.assertEqual(response.status_code, 200)
            self.assertEqual(events[-1]["event"], "result")
            self.assertEqual(events[0]["event"], "job")
            self.assertTrue(
                any(event["event"] == "progress" and event["step"] == "extract" for event in events)
            )


class CompositeRegionPackageBuilderTests(unittest.TestCase):
    def test_runs_all_build_steps(self) -> None:
        calls: list[str] = []

        class Extractor:
            def extract(
                self,
                source_path: Path,
                area: MapArea,
                output_path: Path,
                reporter=None,
            ) -> None:
                calls.append("extract")
                output_path.write_bytes(b"pbf")

        class GraphBuilder:
            def build(self, input_path: Path, output_path: Path) -> None:
                calls.append("graph")
                output_path.write_text("{}", encoding="utf-8")

        class MbtilesBuilder:
            def build(
                self,
                input_path: Path,
                output_path: Path,
                area: MapArea,
                reporter=None,
            ) -> None:
                calls.append("mbtiles")
                output_path.write_bytes(b"mbtiles")

        builder = CompositeRegionPackageBuilder(
            extractor=Extractor(),
            graph_builder=GraphBuilder(),
            mbtiles_builder=MbtilesBuilder(),
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_path = root / "source.osm.pbf"
            source_path.write_bytes(b"source")
            output_dir = root / "output"

            builder.build(
                type("Source", (), {"path": source_path})(),
                MapArea(west=0, south=0, east=1, north=1),
                output_dir,
            )

            self.assertEqual(calls, ["extract", "graph", "mbtiles"])
            self.assertTrue((output_dir / "region.osm.pbf").exists())
            self.assertTrue((output_dir / "graph.json").exists())
            self.assertTrue((output_dir / "map.mbtiles").exists())


class BuildDefaultServiceTests(unittest.TestCase):
    def test_defaults_mbtiles_builder_to_tilemaker_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest_path = root / "manifest.json"
            source_path = root / "south-america.osm.pbf"
            source_path.write_bytes(b"source")
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
            }
            with patch.dict(os.environ, env, clear=False):
                service = build_default_service()

            self.assertEqual(
                service.package_builder.mbtiles_builder.command_template,
                [
                    "tilemaker",
                    "--input",
                    "{input}",
                    "--output",
                    "{output}",
                    "--bbox",
                    "{west},{south},{east},{north}",
                    "--store",
                    "/tmp/tilemaker-store",
                    "--threads",
                    "4",
                    "--config",
                    "/opt/tilemaker/config.json",
                    "--process",
                    "/opt/tilemaker/process.lua",
                ],
            )

    def test_discovers_sources_from_maps_root_when_manifest_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            osm_path = root / "south-america-latest.osm"
            pbf_path = root / "south-america-latest.osm.pbf"
            osm_path.write_text(
                """<?xml version='1.0' encoding='UTF-8'?>
<osm version="0.6" generator="test">
  <node id="1" lat="-25.43" lon="-49.27" />
  <node id="2" lat="-25.40" lon="-49.20" />
  <way id="10">
    <nd ref="1" />
    <nd ref="2" />
    <tag k="highway" v="residential" />
  </way>
</osm>
""",
                encoding="utf-8",
            )
            subprocess.run(
                ["osmium", "cat", str(osm_path), "-o", str(pbf_path), "--overwrite"],
                check=True,
            )

            env = {
                "MAPS_ROOT": str(root),
                "SOURCES_MANIFEST": str(root / "missing-manifest.json"),
            }
            with patch.dict(os.environ, env, clear=False):
                service = build_default_service()

            source = service.source_registry.resolve(
                MapArea(west=-49.26, south=-25.42, east=-49.21, north=-25.41)
            )
            self.assertEqual(source.source_id, "south-america-latest")

    def test_uses_cached_manifest_without_running_osmium(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_path = root / "south-america-latest.osm.pbf"
            source_path.write_bytes(b"source")
            manifest_path = root / "sources" / "manifest.json"
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
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
                                "signature": f"{int(source_path.stat().st_mtime)}-{source_path.stat().st_size}",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            env = {
                "MAPS_ROOT": str(root),
                "SOURCES_MANIFEST": str(manifest_path),
            }
            with patch.dict(os.environ, env, clear=False):
                with patch("app.services.maps.OsmiumSourceInspector.inspect") as inspect:
                    service = build_default_service()

            inspect.assert_not_called()
            source = service.source_registry.resolve(
                MapArea(west=-49.5, south=-25.7, east=-49.0, north=-25.2)
            )
            self.assertEqual(source.source_id, "south-america-latest")

    def test_ensure_sources_manifest_persists_manifest_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            osm_path = root / "south-america-latest.osm"
            pbf_path = root / "south-america-latest.osm.pbf"
            manifest_path = root / "sources" / "manifest.json"
            osm_path.write_text(
                """<?xml version='1.0' encoding='UTF-8'?>
<osm version="0.6" generator="test">
  <node id="1" lat="-25.43" lon="-49.27" />
  <node id="2" lat="-25.40" lon="-49.20" />
  <way id="10">
    <nd ref="1" />
    <nd ref="2" />
    <tag k="highway" v="residential" />
  </way>
</osm>
""",
                encoding="utf-8",
            )
            subprocess.run(
                ["osmium", "cat", str(osm_path), "-o", str(pbf_path), "--overwrite"],
                check=True,
            )

            ensure_sources_manifest(root, manifest_path)

            self.assertTrue(manifest_path.exists())
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["sources"][0]["source_id"], "south-america-latest")


if __name__ == "__main__":
    unittest.main()
