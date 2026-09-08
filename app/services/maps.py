"""Map packaging services."""

import math
import fcntl
import hashlib
import json
import os
import re
import errno
import sqlite3
import shutil
import subprocess
import shlex
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import unquote

from app.helpers.model import ArtifactPaths, MapArea, MapPackageResponse
from app.services.graph import GraphBuilder


@dataclass(frozen=True)
class SourceDefinition:
    source_id: str
    path: Path
    west: float
    south: float
    east: float
    north: float
    priority: int = 100
    signature_override: str | None = None
    download_url: str | None = None
    kind: str = "base"
    region_id: str | None = None
    parent_source_id: str | None = None
    parent_source_signature: str | None = None
    pinned: bool = False

    def contains(self, area: MapArea) -> bool:
        return (
            self.west <= area.west
            and self.south <= area.south
            and self.east >= area.east
            and self.north >= area.north
        )

    def footprint(self) -> float:
        return (self.east - self.west) * (self.north - self.south)

    def signature(self) -> str:
        if self.signature_override:
            return self.signature_override
        stat = self.path.stat()
        return f"{int(stat.st_mtime)}-{stat.st_size}"


class SourceRegistry:
    def __init__(self, sources: list[SourceDefinition]) -> None:
        self.sources = sources

    @classmethod
    def from_file(cls, manifest_path: Path) -> "SourceRegistry":
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        items = payload["sources"] if isinstance(payload, dict) else payload
        sources = []
        for item in items:
            bounds = item.get("bounds", item)
            sources.append(
                SourceDefinition(
                    source_id=item["source_id"] if "source_id" in item else item["id"],
                    path=Path(item["path"]),
                    west=bounds["west"],
                    south=bounds["south"],
                    east=bounds["east"],
                    north=bounds["north"],
                    priority=item.get("priority", 100),
                    signature_override=item.get("signature"),
                    download_url=item.get("download_url")
                    or _default_download_url(
                        item["source_id"] if "source_id" in item else item["id"]
                    ),
                    kind=item.get("kind", "base"),
                    region_id=item.get("region_id"),
                    parent_source_id=item.get("parent_source_id"),
                    parent_source_signature=item.get("parent_source_signature"),
                    pinned=bool(item.get("pinned", False)),
                )
            )
        return cls(sources)

    @classmethod
    def discover(
        cls,
        maps_root: Path,
        inspector=None,
    ) -> "SourceRegistry":
        inspector = inspector or OsmiumSourceInspector()
        sources = []
        for path in sorted(maps_root.rglob("*.osm.pbf")):
            if "cache" in path.parts:
                continue
            bounds = inspector.inspect(path)
            sources.append(
                SourceDefinition(
                    source_id=_source_id_from_path(path),
                    path=path,
                    west=bounds["west"],
                    south=bounds["south"],
                    east=bounds["east"],
                    north=bounds["north"],
                    download_url=_default_download_url(_source_id_from_path(path)),
                )
            )
        if not sources:
            raise FileNotFoundError(f"No .osm.pbf sources found under {maps_root}")
        return cls(sources)

    def to_manifest_payload(self) -> dict:
        return {
            "sources": [
                {
                    "source_id": source.source_id,
                    "path": str(source.path),
                    "bounds": {
                        "west": source.west,
                        "south": source.south,
                        "east": source.east,
                        "north": source.north,
                    },
                    "signature": source.signature(),
                    **({"download_url": source.download_url} if source.download_url else {}),
                    **({"kind": source.kind} if source.kind != "base" else {}),
                    **({"region_id": source.region_id} if source.region_id else {}),
                    **(
                        {"parent_source_id": source.parent_source_id}
                        if source.parent_source_id
                        else {}
                    ),
                    **(
                        {"parent_source_signature": source.parent_source_signature}
                        if source.parent_source_signature
                        else {}
                    ),
                    **({"pinned": True} if source.pinned else {}),
                }
                for source in self.sources
            ]
        }

    def resolve(self, area: MapArea) -> SourceDefinition:
        matches = [source for source in self.sources if source.contains(area)]
        if not matches:
            raise ValueError("No source covers the requested area")
        matches.sort(key=lambda source: (source.footprint(), source.priority, source.source_id))
        return matches[0]


class CacheStore:
    def __init__(
        self,
        root: Path,
        ttl: timedelta,
        scratch_root: Path | None = None,
        snap_degrees: float = 0.0,
    ) -> None:
        self.root = root
        self.ttl = ttl
        self.scratch_root = scratch_root or root
        self.snap_degrees = max(0.0, snap_degrees)

    def package_coordinates(
        self,
        source: SourceDefinition,
        area: MapArea,
        pipeline_version: str,
    ) -> tuple[str, Path]:
        bbox = _normalized_bbox(area)
        digest = hashlib.sha256(
            json.dumps(
                {
                    "bbox": bbox,
                    "min_zoom": area.min_zoom,
                    "pipeline_version": pipeline_version,
                    "source_id": source.source_id,
                    "source_signature": source.signature(),
                },
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()[:8]
        slug = (
            f"z{area.min_zoom}_w{bbox['west']}_s{bbox['south']}_e{bbox['east']}_n{bbox['north']}"
            .replace(".", "_")
        )
        return digest, self.root / source.source_id / f"{digest}__{slug}"

    def cleanup(self) -> int:
        if not self.root.exists():
            return 0

        removed = 0
        for meta_path in self.root.glob("*/*/meta.json"):
            package_dir = meta_path.parent
            if self.is_expired(self.read_meta(package_dir)):
                shutil.rmtree(package_dir, ignore_errors=True)
                removed += 1
        return removed

    def read_meta(self, package_dir: Path) -> dict:
        return json.loads((package_dir / "meta.json").read_text(encoding="utf-8"))

    def find_package_by_cache_key(self, cache_key: str) -> Path | None:
        if not self.root.exists():
            return None

        for package_dir in self.root.glob(f"*/{cache_key}__*"):
            if self.is_ready(package_dir):
                return package_dir
        return None

    def is_ready(self, package_dir: Path) -> bool:
        meta_path = package_dir / "meta.json"
        if not meta_path.exists():
            return False
        meta = self.read_meta(package_dir)
        if self.is_expired(meta):
            return False

        artifacts = meta.get("artifacts", {})
        required = [
            artifacts.get("region_pbf"),
            artifacts.get("mbtiles"),
            artifacts.get("graph"),
            artifacts.get("addresses"),
        ]
        if any(not name for name in required):
            return False
        return all((package_dir / name).exists() for name in required)

    def is_expired(self, meta: dict) -> bool:
        created_at = datetime.fromisoformat(meta["created_at"])
        return datetime.now(UTC) >= created_at + self.ttl

    def build_dir_for(self, package_dir: Path) -> Path:
        try:
            relative = package_dir.relative_to(self.root)
        except ValueError:
            return package_dir.parent / f".{package_dir.name}.building"
        return self.scratch_root / relative.parent / f".{relative.name}.building"

    def promote(self, build_dir: Path, package_dir: Path) -> None:
        if package_dir.exists():
            shutil.rmtree(package_dir)
        package_dir.parent.mkdir(parents=True, exist_ok=True)
        staging_dir = package_dir.parent / f".{package_dir.name}.promoting"
        if staging_dir.exists():
            shutil.rmtree(staging_dir, ignore_errors=True)
        try:
            build_dir.rename(staging_dir)
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise
            shutil.copytree(build_dir, staging_dir)
            shutil.rmtree(build_dir, ignore_errors=True)
        staging_dir.rename(package_dir)

    def remove_build_dir(self, build_dir: Path) -> None:
        if build_dir.exists():
            shutil.rmtree(build_dir, ignore_errors=True)

    @contextmanager
    def lock(self, package_dir: Path):
        package_dir.parent.mkdir(parents=True, exist_ok=True)
        lock_path = package_dir.parent / f"{package_dir.name}.lock"
        with lock_path.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

class ArtifactStageStore:
    def __init__(self, root: Path, pipeline_version: str) -> None:
        self.root = root / "_stages"
        self.pipeline_version = pipeline_version

    def materialize_region(
        self,
        source: SourceDefinition,
        area: MapArea,
        extractor,
        reporter=None,
    ) -> Path:
        stage_dir = self.root / "extract" / source.source_id / self._stage_key("extract", source, area)
        artifact_path = stage_dir / "region.osm.pbf"
        with self._lock(stage_dir):
            if artifact_path.exists():
                _report(reporter, "extract", "Reusing extracted regional PBF")
                return artifact_path

            build_dir = self._build_dir_for(stage_dir)
            self._remove_dir(build_dir)
            build_dir.mkdir(parents=True, exist_ok=True)
            extractor.extract(source.path, area, build_dir / "region.osm.pbf", reporter=reporter)
            self._promote(build_dir, stage_dir)
        return artifact_path

    def materialize_graph(
        self,
        source: SourceDefinition,
        area: MapArea,
        region_path: Path,
        graph_builder,
        reporter=None,
    ) -> Path:
        stage_dir = self.root / "graph" / source.source_id / self._stage_key("graph", source, area)
        artifact_path = stage_dir / "graph.json"
        with self._lock(stage_dir):
            if artifact_path.exists():
                _report(reporter, "graph", "Reusing graph artifact")
                return artifact_path

            build_dir = self._build_dir_for(stage_dir)
            self._remove_dir(build_dir)
            build_dir.mkdir(parents=True, exist_ok=True)
            graph_builder.build(region_path, build_dir / "graph.json", area)
            self._promote(build_dir, stage_dir)
        return artifact_path

    def materialize_addresses(
        self,
        source: SourceDefinition,
        area: MapArea,
        region_path: Path,
        address_builder,
        reporter=None,
    ) -> Path:
        stage_dir = self.root / "addresses" / source.source_id / self._stage_key("addresses", source, area)
        artifact_path = stage_dir / "addresses.sqlite3"
        with self._lock(stage_dir):
            if artifact_path.exists():
                _report(reporter, "addresses", "Reusing address index artifact")
                return artifact_path

            build_dir = self._build_dir_for(stage_dir)
            self._remove_dir(build_dir)
            build_dir.mkdir(parents=True, exist_ok=True)
            address_builder.build(region_path, build_dir / "addresses.sqlite3", area, reporter=reporter)
            self._promote(build_dir, stage_dir)
        return artifact_path

    def _stage_key(self, stage: str, source: SourceDefinition, area: MapArea) -> str:
        bbox = _normalized_bbox(area)
        payload = {
            "stage": stage,
            "bbox": bbox,
            "pipeline_version": self.pipeline_version,
            "source_id": source.source_id,
            "source_signature": source.signature(),
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:16]

    def _build_dir_for(self, stage_dir: Path) -> Path:
        return stage_dir.parent / f".{stage_dir.name}.building"

    def _promote(self, build_dir: Path, stage_dir: Path) -> None:
        if stage_dir.exists():
            shutil.rmtree(stage_dir, ignore_errors=True)
        build_dir.rename(stage_dir)

    def _remove_dir(self, path: Path) -> None:
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)

    @contextmanager
    def _lock(self, stage_dir: Path):
        stage_dir.parent.mkdir(parents=True, exist_ok=True)
        lock_path = stage_dir.parent / f"{stage_dir.name}.lock"
        with lock_path.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class OsmiumRegionExtractor:
    def __init__(self, binary: str = "osmium", strategy: str = "smart") -> None:
        self.binary = binary
        self.strategy = strategy

    def extract(self, source_path: Path, area: MapArea, output_path: Path, reporter=None) -> None:
        _run_command(
            [
                self.binary,
                "extract",
                "--overwrite",
                "--strategy",
                self.strategy,
                "--set-bounds",
                "--bbox",
                f"{area.west},{area.south},{area.east},{area.north}",
                "-o",
                str(output_path),
                str(source_path),
            ],
            reporter=reporter,
            step="extract",
        )


class OsmiumSourceInspector:
    def __init__(self, binary: str = "osmium") -> None:
        self.binary = binary

    def inspect(self, source_path: Path) -> dict[str, float]:
        result = subprocess.run(
            [self.binary, "fileinfo", "-e", "-j", str(source_path)],
            check=True,
            capture_output=True,
            text=True,
        )
        payload = json.loads(result.stdout)
        bbox = payload["data"]["bbox"]
        return {
            "west": bbox[0],
            "south": bbox[1],
            "east": bbox[2],
            "north": bbox[3],
        }


class CommandMbtilesBuilder:
    def __init__(
        self,
        command_template: list[str] | None,
        base_config_path: Path | None = None,
    ) -> None:
        self.command_template = command_template
        self.base_config_path = base_config_path

    def build(self, input_path: Path, output_path: Path, area: MapArea, reporter=None) -> None:
        if not self.command_template:
            raise RuntimeError("MBTiles build command is not configured")
        config_path = self._render_config(output_path.parent, area) if self.base_config_path else None
        command = [
            part.format(
                input=input_path,
                output=output_path,
                west=area.west,
                south=area.south,
                east=area.east,
                north=area.north,
                min_zoom=area.min_zoom,
                config=config_path if config_path is not None else "",
            )
            for part in self.command_template
        ]
        _run_command(command, reporter=reporter, step="mbtiles")

    def _render_config(self, output_dir: Path, area: MapArea) -> Path:
        assert self.base_config_path is not None
        payload = json.loads(self.base_config_path.read_text(encoding="utf-8"))
        settings = payload.setdefault("settings", {})
        settings["minzoom"] = area.min_zoom
        settings["maxzoom"] = max(int(settings.get("maxzoom", area.min_zoom)), area.min_zoom)
        settings["basezoom"] = max(int(settings.get("basezoom", settings["maxzoom"])), area.min_zoom)
        config_path = output_dir / "tilemaker-config.json"
        config_path.write_text(json.dumps(payload), encoding="utf-8")
        return config_path


class AddressIndexBuilder:
    def __init__(self, binary: str = "osmium", location_index: str = "flex_mem") -> None:
        self.binary = binary
        self.location_index = location_index

    def build(self, input_path: Path, output_path: Path, area: MapArea, reporter=None) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if output_path.exists():
            output_path.unlink()

        with sqlite3.connect(output_path) as connection:
            self._create_schema(connection)
            self._write_metadata(connection, input_path, area)
            connection.executemany(
                '''
                INSERT INTO addresses (
                    osm_type,
                    osm_id,
                    lat,
                    lon,
                    display_name,
                    name,
                    street,
                    place,
                    house_number,
                    neighbourhood,
                    suburb,
                    city,
                    state,
                    postcode,
                    country,
                    raw_tags
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                self._iter_address_rows(input_path, area, reporter=reporter),
            )
            connection.commit()

    def _create_schema(self, connection: sqlite3.Connection) -> None:
        connection.executescript(
            '''
            CREATE TABLE metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE addresses (
                osm_type TEXT NOT NULL,
                osm_id INTEGER NOT NULL,
                lat REAL NOT NULL,
                lon REAL NOT NULL,
                display_name TEXT NOT NULL,
                name TEXT,
                street TEXT,
                place TEXT,
                house_number TEXT,
                neighbourhood TEXT,
                suburb TEXT,
                city TEXT,
                state TEXT,
                postcode TEXT,
                country TEXT,
                raw_tags TEXT NOT NULL,
                PRIMARY KEY (osm_type, osm_id)
            );

            CREATE INDEX idx_addresses_lat_lon ON addresses (lat, lon);
            CREATE INDEX idx_addresses_display_name ON addresses (display_name);
            '''
        )

    def _write_metadata(
        self,
        connection: sqlite3.Connection,
        input_path: Path,
        area: MapArea,
    ) -> None:
        metadata = [
            ("schema_version", "1"),
            ("created_at", datetime.now(UTC).isoformat()),
            ("source_path", str(input_path)),
            ("west", f"{area.west:.6f}"),
            ("south", f"{area.south:.6f}"),
            ("east", f"{area.east:.6f}"),
            ("north", f"{area.north:.6f}"),
        ]
        connection.executemany(
            "INSERT INTO metadata (key, value) VALUES (?, ?)",
            metadata,
        )

    def _iter_address_rows(self, input_path: Path, area: MapArea, reporter=None):
        _report(reporter, "addresses", "Building address index")
        for element in self._iter_address_elements(input_path):
            if not _point_in_area(element["lat"], element["lon"], area):
                continue
            address_tags = _address_tags(element["tags"])
            if not address_tags:
                continue
            yield (
                element["osm_type"],
                element["osm_id"],
                round(element["lat"], 6),
                round(element["lon"], 6),
                _display_name_from_address_tags(address_tags),
                element["tags"].get("name"),
                address_tags.get("addr:street"),
                address_tags.get("addr:place"),
                address_tags.get("addr:housenumber"),
                address_tags.get("addr:neighbourhood"),
                address_tags.get("addr:suburb"),
                address_tags.get("addr:city"),
                address_tags.get("addr:state"),
                address_tags.get("addr:postcode"),
                address_tags.get("addr:country"),
                json.dumps(address_tags, sort_keys=True),
            )

    def _iter_address_elements(self, input_path: Path):
        with self._location_index_arg() as location_index:
            command = [
                self.binary,
                "add-locations-to-ways",
                "--no-progress",
                "--ignore-missing-nodes",
                "--index-type",
                location_index,
                "-f",
                "opl,add_metadata=false",
                str(input_path),
            ]
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            assert process.stdout is not None
            assert process.stderr is not None

            try:
                for raw_line in process.stdout:
                    line = raw_line.strip()
                    if not line or line[0] not in {"n", "w"}:
                        continue

                    fields = _parse_opl_fields(line)
                    tags = _parse_tags(fields.get("T", ""))
                    if not _address_tags(tags):
                        continue

                    if line[0] == "n":
                        lon = fields.get("x")
                        lat = fields.get("y")
                        if lon is None or lat is None:
                            continue
                        yield {
                            "osm_type": "node",
                            "osm_id": int(line.split(" ", 1)[0][1:]),
                            "lat": float(lat),
                            "lon": float(lon),
                            "tags": tags,
                        }
                        continue

                    points = _parse_way_points(fields.get("N", ""))
                    if not points:
                        continue
                    lat, lon = _centroid(points)
                    yield {
                        "osm_type": "way",
                        "osm_id": int(line.split(" ", 1)[0][1:]),
                        "lat": lat,
                        "lon": lon,
                        "tags": tags,
                    }
            finally:
                stderr = process.stderr.read()
                return_code = process.wait()
                process.stdout.close()
                process.stderr.close()
                if return_code != 0:
                    raise subprocess.CalledProcessError(
                        return_code,
                        command,
                        output=None,
                        stderr=stderr,
                    )

    @contextmanager
    def _location_index_arg(self):
        if "," in self.location_index or not self.location_index.endswith("_file_array"):
            yield self.location_index
            return

        with tempfile.TemporaryDirectory(prefix="osmium-index-") as tmp:
            yield f"{self.location_index},{Path(tmp) / 'locations.idx'}"


class CompositeRegionPackageBuilder:
    def __init__(
        self,
        extractor,
        graph_builder,
        address_builder,
        mbtiles_builder,
        artifact_store: ArtifactStageStore | None = None,
        parallel_tasks: int = 1,
    ) -> None:
        self.extractor = extractor
        self.graph_builder = graph_builder
        self.address_builder = address_builder
        self.mbtiles_builder = mbtiles_builder
        self.artifact_store = artifact_store
        self.parallel_tasks = max(1, parallel_tasks)

    def build(
        self,
        source: SourceDefinition,
        area: MapArea,
        output_dir: Path,
        reporter=None,
    ) -> None:
        output_dir.mkdir(parents=True, exist_ok=True)
        region_path = output_dir / "region.osm.pbf"
        graph_path = output_dir / "graph.json"
        addresses_path = output_dir / "addresses.sqlite3"
        mbtiles_path = output_dir / "map.mbtiles"
        stage_region_path = region_path
        if self.artifact_store is not None:
            _report(reporter, "extract", "Preparing regional PBF")
            stage_region_path = self.artifact_store.materialize_region(
                source=source,
                area=area,
                extractor=self.extractor,
                reporter=reporter,
            )
        else:
            _report(reporter, "extract", "Extracting regional PBF")
            self.extractor.extract(source.path, area, stage_region_path, reporter=reporter)

        def build_graph() -> Path:
            _report(reporter, "graph", "Building graph")
            if self.artifact_store is None:
                self.graph_builder.build(stage_region_path, graph_path, area)
                return graph_path
            return self.artifact_store.materialize_graph(
                source=source,
                area=area,
                region_path=stage_region_path,
                graph_builder=self.graph_builder,
                reporter=reporter,
            )

        def build_mbtiles() -> None:
            _report(reporter, "mbtiles", "Building MBTiles")
            self.mbtiles_builder.build(stage_region_path, mbtiles_path, area, reporter=reporter)

        def build_addresses() -> Path:
            _report(reporter, "addresses", "Building address index")
            if self.artifact_store is None:
                self.address_builder.build(stage_region_path, addresses_path, area, reporter=reporter)
                return addresses_path
            return self.artifact_store.materialize_addresses(
                source=source,
                area=area,
                region_path=stage_region_path,
                address_builder=self.address_builder,
                reporter=reporter,
            )

        if self.parallel_tasks > 1:
            with ThreadPoolExecutor(max_workers=min(2, self.parallel_tasks)) as executor:
                graph_future = executor.submit(build_graph)
                mbtiles_future = executor.submit(build_mbtiles)
                staged_graph_path = graph_future.result()
                mbtiles_future.result()
        else:
            staged_graph_path = build_graph()
            build_mbtiles()
        staged_addresses_path = build_addresses()

        if stage_region_path != region_path:
            _link_or_copy(stage_region_path, region_path)
        if staged_graph_path != graph_path:
            _link_or_copy(staged_graph_path, graph_path)
        if staged_addresses_path != addresses_path:
            _link_or_copy(staged_addresses_path, addresses_path)

class MapService:
    def __init__(
        self,
        source_registry: SourceRegistry,
        cache_store: CacheStore,
        package_builder,
        pipeline_version: str,
        source_registry_loader=None,
        source_registry_state_getter=None,
        usage_tracker=None,
    ) -> None:
        self.source_registry = source_registry
        self.cache_store = cache_store
        self.package_builder = package_builder
        self.pipeline_version = pipeline_version
        self.source_registry_loader = source_registry_loader
        self.source_registry_state_getter = source_registry_state_getter
        self.usage_tracker = usage_tracker
        self._source_registry_state = (
            source_registry_state_getter() if source_registry_state_getter else None
        )
        self._source_registry_lock = threading.Lock()

    def build_map(self, area: MapArea, reporter=None) -> MapPackageResponse:
        source, effective_area, cache_key, package_dir = self.prepare(area)
        return self.build_prepared(
            source=source,
            area=effective_area,
            cache_key=cache_key,
            package_dir=package_dir,
            reporter=reporter,
        )

    def prepare(self, area: MapArea) -> tuple[SourceDefinition, MapArea, str, Path]:
        self._refresh_source_registry()
        effective_area = _canonical_area(area, self.cache_store.snap_degrees)
        source = self.source_registry.resolve(effective_area)
        cache_key, package_dir = self.cache_store.package_coordinates(
            source=source,
            area=effective_area,
            pipeline_version=self.pipeline_version,
        )
        return source, effective_area, cache_key, package_dir

    def track_usage(self, area: MapArea, source: SourceDefinition) -> None:
        if self.usage_tracker is None:
            return
        self.usage_tracker.record_request(area, source)

    def _refresh_source_registry(self) -> None:
        if not self.source_registry_loader or not self.source_registry_state_getter:
            return

        current_state = self.source_registry_state_getter()
        if current_state == self._source_registry_state:
            return

        with self._source_registry_lock:
            current_state = self.source_registry_state_getter()
            if current_state == self._source_registry_state:
                return
            self.source_registry = self.source_registry_loader()
            self._source_registry_state = current_state

    def build_prepared(
        self,
        source: SourceDefinition,
        area: MapArea,
        cache_key: str,
        package_dir: Path,
        reporter=None,
    ) -> MapPackageResponse:
        _report(reporter, "source", f"Resolved source {source.source_id}")
        _report(reporter, "cache", f"Using cache key {cache_key}")

        with self.cache_store.lock(package_dir):
            _report(reporter, "lock", "Acquired package lock")
            if self.cache_store.is_ready(package_dir):
                _report(reporter, "cache", "Cache hit")
                return self._response_for(
                    cache_key=cache_key,
                    source=source,
                    package_dir=package_dir,
                    cached=True,
                )

            build_dir = self.cache_store.build_dir_for(package_dir)
            self.cache_store.remove_build_dir(build_dir)

            try:
                self.package_builder.build(source, area, build_dir, reporter=reporter)
                self._write_meta(cache_key, source, area, build_dir)
                self.cache_store.promote(build_dir, package_dir)
            except Exception:
                self.cache_store.remove_build_dir(build_dir)
                raise

            return self._response_for(
                cache_key=cache_key,
                source=source,
                package_dir=package_dir,
                cached=False,
            )

    def _write_meta(
        self,
        cache_key: str,
        source: SourceDefinition,
        area: MapArea,
        package_dir: Path,
    ) -> None:
        created_at = datetime.now(UTC)
        meta = {
            "cache_key": cache_key,
            "created_at": created_at.isoformat(),
            "expires_at": (created_at + self.cache_store.ttl).isoformat(),
            "pipeline_version": self.pipeline_version,
            "source_id": source.source_id,
            "source_signature": source.signature(),
            "min_zoom": area.min_zoom,
            "bbox": {
                "west": area.west,
                "south": area.south,
                "east": area.east,
                "north": area.north,
            },
            "artifacts": {
                "region_pbf": "region.osm.pbf",
                "mbtiles": "map.mbtiles",
                "graph": "graph.json",
                "addresses": "addresses.sqlite3",
            },
        }
        (package_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")

    def _response_for(
        self,
        cache_key: str,
        source: SourceDefinition,
        package_dir: Path,
        cached: bool,
    ) -> MapPackageResponse:
        meta = self.cache_store.read_meta(package_dir)
        return MapPackageResponse(
            status="ready",
            cached=cached,
            cache_key=cache_key,
            source_id=source.source_id,
            expires_at=meta["expires_at"],
            artifacts=ArtifactPaths(
                region_pbf=str(package_dir / "region.osm.pbf"),
                mbtiles=str(package_dir / "map.mbtiles"),
                graph=str(package_dir / "graph.json"),
                addresses=str(package_dir / "addresses.sqlite3"),
                meta=str(package_dir / "meta.json"),
            ),
        )


def map_service() -> MapService:
    """Build the default map service from environment settings."""

    maps_root = Path(os.getenv("MAPS_ROOT", "/osm_maps"))
    manifest_path = Path(
        os.getenv("SOURCES_MANIFEST", str(maps_root / "sources" / "manifest.json"))
    )
    ttl_hours = int(os.getenv("CACHE_TTL_HOURS", "1"))
    snap_degrees = float(os.getenv("CACHE_KEY_SNAP_DEGREES", "0.001"))
    pipeline_version = os.getenv("PIPELINE_VERSION", "v3-address-index")
    scratch_root = Path(
        os.getenv(
            "BUILD_SCRATCH_ROOT",
            str(Path(tempfile.gettempdir()) / "homelander-builds"),
        )
    )
    osmium_location_index = os.getenv("OSMIUM_LOCATION_INDEX", "flex_mem")
    osmium_extract_strategy = os.getenv("OSMIUM_EXTRACT_STRATEGY", "smart")
    tilemaker_threads = os.getenv("TILEMAKER_THREADS", str(max(1, min(6, os.cpu_count() or 1))))
    parallel_tasks = int(os.getenv("PIPELINE_PARALLEL_TASKS", "1"))
    tilemaker_command = [
        os.getenv("TILEMAKER_BINARY", "tilemaker"),
        "--input",
        "{input}",
        "--output",
        "{output}",
        "--bbox",
        "{west},{south},{east},{north}",
        "--store",
        os.getenv("TILEMAKER_STORE", "/tmp/tilemaker-store"),
        "--threads",
        tilemaker_threads,
        "--config",
        "{config}",
        "--process",
        os.getenv("TILEMAKER_PROCESS", "/opt/tilemaker/process.lua"),
    ]
    mbtiles_command = os.getenv("MBTILES_COMMAND")
    source_registry = _load_source_registry(
        maps_root=maps_root,
        manifest_path=manifest_path,
        inspector=OsmiumSourceInspector(binary=os.getenv("OSMIUM_BINARY", "osmium")),
    )
    return MapService(
        source_registry=source_registry,
        cache_store=CacheStore(
            maps_root / "cache",
            ttl=timedelta(hours=ttl_hours),
            scratch_root=scratch_root,
            snap_degrees=snap_degrees,
        ),
        package_builder=CompositeRegionPackageBuilder(
            extractor=OsmiumRegionExtractor(
                binary=os.getenv("OSMIUM_BINARY", "osmium"),
                strategy=osmium_extract_strategy,
            ),
            graph_builder=GraphBuilder(
                binary=os.getenv("OSMIUM_BINARY", "osmium"),
                location_index=osmium_location_index,
            ),
            address_builder=AddressIndexBuilder(
                binary=os.getenv("OSMIUM_BINARY", "osmium"),
                location_index=osmium_location_index,
            ),
            mbtiles_builder=CommandMbtilesBuilder(
                shlex.split(mbtiles_command) if mbtiles_command else tilemaker_command,
                base_config_path=(
                    None
                    if mbtiles_command
                    else Path(os.getenv("TILEMAKER_CONFIG", "/opt/tilemaker/config.json"))
                ),
            ),
            artifact_store=ArtifactStageStore(maps_root / "cache", pipeline_version=pipeline_version),
            parallel_tasks=parallel_tasks,
        ),
        pipeline_version=pipeline_version,
        source_registry_loader=lambda: _load_source_registry(
            maps_root=maps_root,
            manifest_path=manifest_path,
            inspector=OsmiumSourceInspector(binary=os.getenv("OSMIUM_BINARY", "osmium")),
        ),
        source_registry_state_getter=lambda: _path_state_token(manifest_path),
    )


def _normalized_bbox(area: MapArea) -> dict[str, str]:
    return {
        "west": f"{area.west:.6f}",
        "south": f"{area.south:.6f}",
        "east": f"{area.east:.6f}",
        "north": f"{area.north:.6f}",
    }


def _canonical_area(area: MapArea, snap_degrees: float) -> MapArea:
    if snap_degrees <= 0:
        return area
    return MapArea(
        west=max(-180.0, _snap_down(area.west, snap_degrees)),
        south=max(-90.0, _snap_down(area.south, snap_degrees)),
        east=min(180.0, _snap_up(area.east, snap_degrees)),
        north=min(90.0, _snap_up(area.north, snap_degrees)),
        min_zoom=area.min_zoom,
    )


def _snap_down(value: float, step: float) -> float:
    return round(math.floor(value / step) * step, 6)


def _snap_up(value: float, step: float) -> float:
    return round(math.ceil(value / step) * step, 6)


def _source_id_from_path(path: Path) -> str:
    name = path.name
    if name.endswith(".osm.pbf"):
        return name[: -len(".osm.pbf")]
    return path.stem


def _report(reporter, step: str, message: str) -> None:
    if reporter:
        reporter(step, message)


def _run_command(command: list[str], reporter=None, step: str | None = None) -> None:
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert process.stdout is not None
    for raw_line in process.stdout:
        line = raw_line.strip()
        if line:
            _report(reporter, step or "command", line)
    return_code = process.wait()
    if return_code != 0:
        raise subprocess.CalledProcessError(return_code, command)


def _parse_opl_fields(line: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for raw_field in line.split(" ")[1:]:
        if not raw_field:
            continue
        fields[raw_field[0]] = raw_field[1:]
    return fields


def _parse_tags(raw_tags: str) -> dict[str, str]:
    if not raw_tags:
        return {}

    tags: dict[str, str] = {}
    for item in raw_tags.split(","):
        key, separator, value = item.partition("=")
        if not separator:
            continue
        tags[_decode_opl_string(key)] = _decode_opl_string(value)
    return tags


def _decode_opl_string(raw_value: str) -> str:
    normalized = re.sub(r"%([0-9A-Fa-f]{2})%", r"%\1", raw_value)
    return unquote(normalized)


def _parse_way_points(raw_nodes: str) -> list[tuple[int, float, float]]:
    points: list[tuple[int, float, float]] = []
    for raw_node in raw_nodes.split(","):
        node_ref, _, remainder = raw_node.partition("x")
        if not remainder or not node_ref.startswith("n"):
            continue
        lon, separator, lat = remainder.partition("y")
        if not separator:
            continue
        points.append((int(node_ref[1:]), float(lat), float(lon)))
    return points


def _centroid(points: list[tuple[int, float, float]]) -> tuple[float, float]:
    lat_sum = sum(point[1] for point in points)
    lon_sum = sum(point[2] for point in points)
    size = len(points)
    return lat_sum / size, lon_sum / size


def _point_in_area(lat: float, lon: float, area: MapArea) -> bool:
    return area.west <= lon <= area.east and area.south <= lat <= area.north


def _address_tags(tags: dict[str, str]) -> dict[str, str]:
    return {
        key: value
        for key, value in tags.items()
        if key.startswith("addr:") and value
    }


def _display_name_from_address_tags(tags: dict[str, str]) -> str:
    street = tags.get("addr:street") or tags.get("addr:place")
    house_number = tags.get("addr:housenumber")
    parts: list[str] = []
    if street and house_number:
        parts.append(f"{street}, {house_number}")
    elif street:
        parts.append(street)
    elif house_number:
        parts.append(house_number)

    for key in (
        "addr:neighbourhood",
        "addr:suburb",
        "addr:city",
        "addr:state",
        "addr:postcode",
        "addr:country",
    ):
        value = tags.get(key)
        if value:
            parts.append(value)

    return ", ".join(parts) if parts else "address"


def _load_source_registry(
    maps_root: Path,
    manifest_path: Path,
    inspector: OsmiumSourceInspector,
) -> SourceRegistry:
    if manifest_path.exists():
        return SourceRegistry.from_file(manifest_path)

    return ensure_sources_manifest(
        maps_root=maps_root,
        manifest_path=manifest_path,
        inspector=inspector,
    )


def ensure_sources_manifest(
    maps_root: Path,
    manifest_path: Path,
    inspector: OsmiumSourceInspector | None = None,
) -> SourceRegistry:
    """Persist a source manifest for the current maps root."""

    if manifest_path.exists():
        registry = SourceRegistry.from_file(manifest_path)
        if _registry_matches_filesystem(registry):
            return registry

    inspector = inspector or OsmiumSourceInspector()
    registry = SourceRegistry.discover(maps_root, inspector=inspector)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(registry.to_manifest_payload()),
        encoding="utf-8",
    )
    return registry


def _registry_matches_filesystem(registry: SourceRegistry) -> bool:
    for source in registry.sources:
        if not source.path.exists():
            return False
        if source.signature_override and source.signature_override != source.signature():
            return False
    return True


def _default_download_url(source_id: str) -> str | None:
    base_url = os.getenv("SOURCE_DOWNLOAD_BASE_URL", "https://download.geofabrik.de").strip()
    if not base_url:
        return None
    normalized_source_id = source_id.removesuffix(".osm.pbf")
    if not normalized_source_id.endswith("-latest"):
        return None
    return f"{base_url.rstrip('/')}/{normalized_source_id}.osm.pbf"


def _path_state_token(path: Path) -> tuple[int, int] | None:
    if not path.exists():
        return None
    stat = path.stat()
    return (stat.st_mtime_ns, stat.st_size)


def _link_or_copy(source: Path, destination: Path) -> None:
    if destination.exists():
        destination.unlink()
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, destination)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        shutil.copy2(source, destination)
