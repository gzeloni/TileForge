import json
import os
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.app import make_app
from app.helpers.model import MapArea
from app.services.jobs import InlineMapJobService
from app.services.maps import (
    ArtifactStageStore,
    CacheStore,
    CommandMbtilesBuilder,
    CompositeRegionPackageBuilder,
    MapService,
    SourceRegistry,
    SourceDefinition,
    map_service,
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

    def test_reuses_containing_cached_package_within_offset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = CacheStore(
                root / "cache",
                ttl=timedelta(hours=12),
                bbox_offset=0.02,
            )
            package_dir = root / "cache" / "south-america" / "abc"
            package_dir.mkdir(parents=True)
            (package_dir / "region.osm.pbf").write_bytes(b"pbf")
            (package_dir / "map.mbtiles").write_bytes(b"mbtiles")
            (package_dir / "graph.json").write_text("{}", encoding="utf-8")
            (package_dir / "meta.json").write_text(
                json.dumps(
                    {
                        "created_at": datetime.now(UTC).isoformat(),
                        "expires_at": (datetime.now(UTC) + timedelta(hours=12)).isoformat(),
                        "bbox": {
                            "west": -46.70,
                            "south": -23.62,
                            "east": -46.56,
                            "north": -23.50,
                        },
                        "artifacts": {
                            "region_pbf": "region.osm.pbf",
                            "mbtiles": "map.mbtiles",
                            "graph": "graph.json",
                        },
                    }
                ),
                encoding="utf-8",
            )

            reusable = store.find_reusable_package(
                source_id="south-america",
                area=MapArea(west=-46.70, south=-23.62, east=-46.56, north=-23.51),
            )

            self.assertEqual(reusable, package_dir)

    def test_snaps_nearby_bboxes_to_same_package_coordinates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = CacheStore(
                root / "cache",
                ttl=timedelta(hours=12),
                bbox_grid=0.02,
            )
            source_path = root / "south-america.osm.pbf"
            source_path.write_bytes(b"source")
            source = SourceDefinition(
                source_id="south-america",
                path=source_path,
                west=-92.0,
                south=-57.0,
                east=-27.0,
                north=15.0,
            )

            first_key, first_dir = store.package_coordinates(
                source=source,
                area=MapArea(west=-46.70, south=-23.62, east=-46.56, north=-23.50),
                pipeline_version="v1",
            )
            second_key, second_dir = store.package_coordinates(
                source=source,
                area=MapArea(west=-46.70, south=-23.62, east=-46.56, north=-23.51),
                pipeline_version="v1",
            )

            self.assertEqual(first_key, second_key)
            self.assertEqual(first_dir, second_dir)


class MapApiTests(unittest.TestCase):
    API_KEY = "test-api-key"

    def test_post_packages_returns_package_id_and_ready_links(self) -> None:
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
            client = TestClient(make_app(service=service, api_key=self.API_KEY))
            payload = {
                "west": -46.70,
                "south": -23.62,
                "east": -46.56,
                "north": -23.50,
            }

            submitted = client.post(
                "/api/map/packages",
                json=payload,
                headers={"Authorization": f"Bearer {self.API_KEY}"},
            )

            self.assertEqual(submitted.status_code, 202)
            self.assertIn("package_id", submitted.json())
            self.assertIn(submitted.json()["status"], {"queued", "building", "ready"})
            self.assertEqual(submitted.json()["source_id"], "south-america")
            self.assertEqual(
                submitted.json()["artifacts"]["mbtiles"],
                f"/api/map/packages/{submitted.json()['package_id']}/artifacts/mbtiles",
            )
            for _ in range(10):
                status = client.get(
                    f"/api/map/packages/{submitted.json()['package_id']}",
                    headers={"Authorization": f"Bearer {self.API_KEY}"},
                )
                if status.json()["status"] == "ready":
                    break

    def test_get_package_status_returns_ready_without_job_lookup(self) -> None:
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
            client = TestClient(make_app(service=service, api_key=self.API_KEY))

            submitted = client.post(
                "/api/map/packages",
                json={
                    "west": -46.70,
                    "south": -23.62,
                    "east": -46.56,
                    "north": -23.50,
                },
                headers={"Authorization": f"Bearer {self.API_KEY}"},
            )
            package_id = submitted.json()["package_id"]
            package_status = None
            for _ in range(10):
                package_status = client.get(
                    f"/api/map/packages/{package_id}",
                    headers={"Authorization": f"Bearer {self.API_KEY}"},
                )
                if package_status.json()["status"] == "ready":
                    break

            self.assertEqual(package_status.status_code, 200)
            self.assertEqual(package_status.json()["status"], "ready")
            self.assertEqual(package_status.json()["package_id"], package_id)
            self.assertEqual(
                package_status.json()["artifacts"]["graph"],
                f"/api/map/packages/{package_id}/artifacts/graph",
            )

    def test_downloads_mbtiles_artifact_from_package_id(self) -> None:
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
            client = TestClient(make_app(service=service, api_key=self.API_KEY))

            submitted = client.post(
                "/api/map/packages",
                json={
                    "west": -46.70,
                    "south": -23.62,
                    "east": -46.56,
                    "north": -23.50,
                },
                headers={"Authorization": f"Bearer {self.API_KEY}"},
            )
            package_id = submitted.json()["package_id"]
            for _ in range(10):
                status = client.get(
                    f"/api/map/packages/{package_id}",
                    headers={"Authorization": f"Bearer {self.API_KEY}"},
                )
                if status.json()["status"] == "ready":
                    break

            artifact = client.get(
                f"/api/map/packages/{package_id}/artifacts/mbtiles",
                headers={"Authorization": f"Bearer {self.API_KEY}"},
            )

            self.assertEqual(artifact.status_code, 200)
            self.assertEqual(artifact.content, b"mbtiles")

    def test_different_min_zoom_creates_different_package(self) -> None:
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
            client = TestClient(make_app(service=service, api_key=self.API_KEY))

            first = client.post(
                "/api/map/packages",
                json={
                    "west": -46.70,
                    "south": -23.62,
                    "east": -46.56,
                    "north": -23.50,
                    "minZoom": 5,
                },
                headers={"Authorization": f"Bearer {self.API_KEY}"},
            )
            first_package_id = first.json()["package_id"]
            for _ in range(10):
                first_status = client.get(
                    f"/api/map/packages/{first_package_id}",
                    headers={"Authorization": f"Bearer {self.API_KEY}"},
                )
                if first_status.json()["status"] == "ready":
                    break

            second = client.post(
                "/api/map/packages",
                json={
                    "west": -46.70,
                    "south": -23.62,
                    "east": -46.56,
                    "north": -23.50,
                    "minZoom": 6,
                },
                headers={"Authorization": f"Bearer {self.API_KEY}"},
            )
            second_package_id = second.json()["package_id"]
            for _ in range(10):
                second_status = client.get(
                    f"/api/map/packages/{second_package_id}",
                    headers={"Authorization": f"Bearer {self.API_KEY}"},
                )
                if second_status.json()["status"] == "ready":
                    break

            self.assertEqual(builder.calls, 2)
            self.assertNotEqual(first_package_id, second_package_id)

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
            client = TestClient(make_app(service=service, api_key=self.API_KEY))
            payload = {
                "west": -49.5,
                "south": -25.7,
                "east": -49.0,
                "north": -25.2,
            }

            submitted = client.post(
                "/api/map",
                json=payload,
                headers={"Authorization": f"Bearer {self.API_KEY}"},
            )
            job_id = submitted.json()["job_id"]
            status = None
            for _ in range(10):
                status = client.get(
                    f"/api/map/jobs/{job_id}",
                    headers={"Authorization": f"Bearer {self.API_KEY}"},
                )
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
            client = TestClient(make_app(service=service, api_key=self.API_KEY))
            payload = {
                "west": -49.5,
                "south": -25.7,
                "east": -49.0,
                "north": -25.2,
            }

            with client.stream(
                "POST",
                "/api/map/stream",
                json=payload,
                headers={"Authorization": f"Bearer {self.API_KEY}"},
            ) as response:
                events = [json.loads(line) for line in response.iter_lines() if line]

            self.assertEqual(response.status_code, 200)
            self.assertEqual(events[-1]["event"], "result")
            self.assertEqual(events[0]["event"], "job")
            self.assertTrue(
                any(event["event"] == "progress" and event["step"] == "extract" for event in events)
            )

    def test_rejects_requests_without_api_key(self) -> None:
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
            client = TestClient(make_app(service=service, api_key=self.API_KEY))

            response = client.post(
                "/api/map",
                json={
                    "west": -49.5,
                    "south": -25.7,
                    "east": -49.0,
                    "north": -25.2,
                },
            )

            self.assertEqual(response.status_code, 401)

    def test_reuses_larger_cached_bbox_for_nearby_request(self) -> None:
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
            cache_store = CacheStore(root / "cache", ttl=timedelta(hours=12), bbox_offset=0.02)
            builder = FakeRegionPackageBuilder()
            map_service = MapService(
                source_registry=registry,
                cache_store=cache_store,
                package_builder=builder,
                pipeline_version="test-v1",
            )
            service = InlineMapJobService(map_service=map_service)
            client = TestClient(make_app(service=service, api_key=self.API_KEY))

            first = client.post(
                "/api/map",
                json={
                    "west": -46.70,
                    "south": -23.62,
                    "east": -46.56,
                    "north": -23.50,
                },
                headers={"Authorization": f"Bearer {self.API_KEY}"},
            )
            first_job_id = first.json()["job_id"]
            for _ in range(10):
                first_status = client.get(
                    f"/api/map/jobs/{first_job_id}",
                    headers={"Authorization": f"Bearer {self.API_KEY}"},
                )
                if first_status.json()["status"] == "succeeded":
                    break

            second = client.post(
                "/api/map",
                json={
                    "west": -46.70,
                    "south": -23.62,
                    "east": -46.56,
                    "north": -23.51,
                },
                headers={"Authorization": f"Bearer {self.API_KEY}"},
            )
            second_job_id = second.json()["job_id"]
            for _ in range(10):
                second_status = client.get(
                    f"/api/map/jobs/{second_job_id}",
                    headers={"Authorization": f"Bearer {self.API_KEY}"},
                )
                if second_status.json()["status"] == "succeeded":
                    break

            self.assertEqual(builder.calls, 1)
            self.assertTrue(second_status.json()["result"]["cached"])

    def test_reuses_same_snapped_grid_without_reprocessing(self) -> None:
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
            cache_store = CacheStore(
                root / "cache",
                ttl=timedelta(hours=12),
                bbox_offset=0.0,
                bbox_grid=0.02,
            )
            builder = FakeRegionPackageBuilder()
            map_service = MapService(
                source_registry=registry,
                cache_store=cache_store,
                package_builder=builder,
                pipeline_version="test-v1",
            )
            service = InlineMapJobService(map_service=map_service)
            client = TestClient(make_app(service=service, api_key=self.API_KEY))

            first = client.post(
                "/api/map",
                json={
                    "west": -46.70,
                    "south": -23.62,
                    "east": -46.56,
                    "north": -23.50,
                },
                headers={"Authorization": f"Bearer {self.API_KEY}"},
            )
            first_job_id = first.json()["job_id"]
            for _ in range(10):
                first_status = client.get(
                    f"/api/map/jobs/{first_job_id}",
                    headers={"Authorization": f"Bearer {self.API_KEY}"},
                )
                if first_status.json()["status"] == "succeeded":
                    break

            second = client.post(
                "/api/map",
                json={
                    "west": -46.70,
                    "south": -23.62,
                    "east": -46.56,
                    "north": -23.51,
                },
                headers={"Authorization": f"Bearer {self.API_KEY}"},
            )
            second_job_id = second.json()["job_id"]
            for _ in range(10):
                second_status = client.get(
                    f"/api/map/jobs/{second_job_id}",
                    headers={"Authorization": f"Bearer {self.API_KEY}"},
                )
                if second_status.json()["status"] == "succeeded":
                    break

            self.assertEqual(builder.calls, 1)
            self.assertTrue(second_status.json()["result"]["cached"])

    def test_downloads_mbtiles_artifact_from_completed_job(self) -> None:
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
            client = TestClient(make_app(service=service, api_key=self.API_KEY))

            submitted = client.post(
                "/api/map",
                json={
                    "west": -46.70,
                    "south": -23.62,
                    "east": -46.56,
                    "north": -23.50,
                },
                headers={"Authorization": f"Bearer {self.API_KEY}"},
            )
            job_id = submitted.json()["job_id"]
            for _ in range(10):
                status = client.get(
                    f"/api/map/jobs/{job_id}",
                    headers={"Authorization": f"Bearer {self.API_KEY}"},
                )
                if status.json()["status"] == "succeeded":
                    break

            artifact = client.get(
                f"/api/map/jobs/{job_id}/artifacts/mbtiles",
                headers={"Authorization": f"Bearer {self.API_KEY}"},
            )

            self.assertEqual(artifact.status_code, 200)
            self.assertEqual(artifact.content, b"mbtiles")

    def test_downloads_mbtiles_artifact_from_singular_artifact_route(self) -> None:
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
            client = TestClient(make_app(service=service, api_key=self.API_KEY))

            submitted = client.post(
                "/api/map",
                json={
                    "west": -46.70,
                    "south": -23.62,
                    "east": -46.56,
                    "north": -23.50,
                },
                headers={"Authorization": f"Bearer {self.API_KEY}"},
            )
            job_id = submitted.json()["job_id"]
            for _ in range(10):
                status = client.get(
                    f"/api/map/jobs/{job_id}",
                    headers={"Authorization": f"Bearer {self.API_KEY}"},
                )
                if status.json()["status"] == "succeeded":
                    break

            artifact = client.get(
                f"/api/map/jobs/{job_id}/artifact/mbtiles",
                headers={"Authorization": f"Bearer {self.API_KEY}"},
            )

            self.assertEqual(artifact.status_code, 200)
            self.assertEqual(artifact.content, b"mbtiles")


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

    def test_reuses_extract_and_graph_for_different_min_zoom(self) -> None:
        calls = {"extract": 0, "graph": 0, "mbtiles": 0}

        class Extractor:
            def extract(self, source_path: Path, area: MapArea, output_path: Path, reporter=None) -> None:
                calls["extract"] += 1
                output_path.write_bytes(b"pbf")

        class GraphBuilder:
            def build(self, input_path: Path, output_path: Path) -> None:
                calls["graph"] += 1
                output_path.write_text("{}", encoding="utf-8")

        class MbtilesBuilder:
            def build(self, input_path: Path, output_path: Path, area: MapArea, reporter=None) -> None:
                calls["mbtiles"] += 1
                output_path.write_bytes(f"mbtiles-{area.min_zoom}".encode("utf-8"))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_path = root / "south-america-latest.osm.pbf"
            source_path.write_bytes(b"source")
            artifact_store = ArtifactStageStore(root / "cache", pipeline_version="v1")
            builder = CompositeRegionPackageBuilder(
                extractor=Extractor(),
                graph_builder=GraphBuilder(),
                mbtiles_builder=MbtilesBuilder(),
                artifact_store=artifact_store,
                parallel_tasks=2,
            )
            source = type("Source", (), {"path": source_path, "source_id": "south-america-latest", "signature": lambda self: "sig"})()

            first_output = root / "out-5"
            second_output = root / "out-6"
            builder.build(
                source,
                MapArea(west=0, south=0, east=1, north=1, minZoom=5),
                first_output,
            )
            builder.build(
                source,
                MapArea(west=0, south=0, east=1, north=1, minZoom=6),
                second_output,
            )

            self.assertEqual(calls, {"extract": 1, "graph": 1, "mbtiles": 2})
            self.assertTrue((first_output / "region.osm.pbf").exists())
            self.assertTrue((second_output / "graph.json").exists())


class CommandMbtilesBuilderTests(unittest.TestCase):
    def test_renders_tilemaker_config_with_requested_min_zoom(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_path = root / "region.osm.pbf"
            input_path.write_bytes(b"pbf")
            output_path = root / "map.mbtiles"
            base_config_path = root / "config.json"
            base_config_path.write_text(
                json.dumps(
                    {
                        "layers": {"roads": {"minzoom": 12, "maxzoom": 14}},
                        "settings": {"minzoom": 0, "maxzoom": 14, "basezoom": 14},
                    }
                ),
                encoding="utf-8",
            )

            builder = CommandMbtilesBuilder(
                command_template=[
                    "tilemaker",
                    "--input",
                    "{input}",
                    "--output",
                    "{output}",
                    "--bbox",
                    "{west},{south},{east},{north}",
                    "--config",
                    "{config}",
                    "--process",
                    "/opt/tilemaker/process.lua",
                ],
                base_config_path=base_config_path,
            )

            captured = {}

            def fake_run(command, reporter=None, step=None):
                captured["command"] = command

            with patch("app.services.maps._run_command", side_effect=fake_run):
                builder.build(
                    input_path=input_path,
                    output_path=output_path,
                    area=MapArea(west=-46.7, south=-23.62, east=-46.56, north=-23.50, minZoom=5),
                )

            generated_config = Path(captured["command"][8])
            payload = json.loads(generated_config.read_text(encoding="utf-8"))
            self.assertEqual(payload["settings"]["minzoom"], 5)
            self.assertEqual(payload["settings"]["maxzoom"], 14)
            self.assertEqual(payload["settings"]["basezoom"], 14)


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
                service = map_service()

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
                    str(max(1, min(6, os.cpu_count() or 1))),
                    "--config",
                    "{config}",
                    "--process",
                    "/opt/tilemaker/process.lua",
                ],
            )
            self.assertEqual(service.package_builder.parallel_tasks, 2)

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
                service = map_service()

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
                    service = map_service()

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

    def test_prepare_refreshes_source_registry_when_manifest_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first_source_path = root / "south-america-latest.osm.pbf"
            second_source_path = root / "central-america-latest.osm.pbf"
            first_source_path.write_bytes(b"source-1")
            second_source_path.write_bytes(b"source-2")
            manifest_path = root / "sources" / "manifest.json"
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text(
                json.dumps(
                    {
                        "sources": [
                            {
                                "source_id": "south-america-latest",
                                "path": str(first_source_path),
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
                service = map_service()

            manifest_path.write_text(
                json.dumps(
                    {
                        "sources": [
                            {
                                "source_id": "central-america-latest",
                                "path": str(second_source_path),
                                "bounds": {
                                    "west": -118.0,
                                    "south": 5.0,
                                    "east": -74.0,
                                    "north": 33.0,
                                },
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            source, _, _, _ = service.prepare(
                MapArea(west=-90.0, south=10.0, east=-80.0, north=20.0)
            )

            self.assertEqual(source.source_id, "central-america-latest")


if __name__ == "__main__":
    unittest.main()
