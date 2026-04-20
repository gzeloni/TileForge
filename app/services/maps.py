import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import shlex
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

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
                    download_url=item.get("download_url"),
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
    def __init__(self, root: Path, ttl: timedelta) -> None:
        self.root = root
        self.ttl = ttl

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
                    "pipeline_version": pipeline_version,
                    "source_id": source.source_id,
                    "source_signature": source.signature(),
                },
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()[:8]
        slug = (
            f"w{bbox['west']}_s{bbox['south']}_e{bbox['east']}_n{bbox['north']}"
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
        ]
        if any(not name for name in required):
            return False
        return all((package_dir / name).exists() for name in required)

    def is_expired(self, meta: dict) -> bool:
        created_at = datetime.fromisoformat(meta["created_at"])
        return datetime.now(UTC) >= created_at + self.ttl

    def build_dir_for(self, package_dir: Path) -> Path:
        return package_dir.parent / f".{package_dir.name}.building"

    def promote(self, build_dir: Path, package_dir: Path) -> None:
        if package_dir.exists():
            shutil.rmtree(package_dir)
        build_dir.rename(package_dir)

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


class OsmiumRegionExtractor:
    def __init__(self, binary: str = "osmium") -> None:
        self.binary = binary

    def extract(self, source_path: Path, area: MapArea, output_path: Path, reporter=None) -> None:
        _run_command(
            [
                self.binary,
                "extract",
                "--overwrite",
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
    def __init__(self, command_template: list[str] | None) -> None:
        self.command_template = command_template

    def build(self, input_path: Path, output_path: Path, area: MapArea, reporter=None) -> None:
        if not self.command_template:
            raise RuntimeError("MBTiles build command is not configured")
        command = [
            part.format(
                input=input_path,
                output=output_path,
                west=area.west,
                south=area.south,
                east=area.east,
                north=area.north,
            )
            for part in self.command_template
        ]
        _run_command(command, reporter=reporter, step="mbtiles")


class CompositeRegionPackageBuilder:
    def __init__(self, extractor, graph_builder, mbtiles_builder) -> None:
        self.extractor = extractor
        self.graph_builder = graph_builder
        self.mbtiles_builder = mbtiles_builder

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
        mbtiles_path = output_dir / "map.mbtiles"
        _report(reporter, "extract", "Extracting regional PBF")
        self.extractor.extract(source.path, area, region_path, reporter=reporter)
        _report(reporter, "graph", "Building graph")
        self.graph_builder.build(region_path, graph_path)
        _report(reporter, "mbtiles", "Building MBTiles")
        self.mbtiles_builder.build(region_path, mbtiles_path, area, reporter=reporter)

class MapService:
    def __init__(
        self,
        source_registry: SourceRegistry,
        cache_store: CacheStore,
        package_builder,
        pipeline_version: str,
    ) -> None:
        self.source_registry = source_registry
        self.cache_store = cache_store
        self.package_builder = package_builder
        self.pipeline_version = pipeline_version

    def build_map(self, area: MapArea, reporter=None) -> MapPackageResponse:
        source, cache_key, package_dir = self.prepare(area)
        return self.build_prepared(
            source=source,
            area=area,
            cache_key=cache_key,
            package_dir=package_dir,
            reporter=reporter,
        )

    def prepare(self, area: MapArea) -> tuple[SourceDefinition, str, Path]:
        source = self.source_registry.resolve(area)
        cache_key, package_dir = self.cache_store.package_coordinates(
            source=source,
            area=area,
            pipeline_version=self.pipeline_version,
        )
        return source, cache_key, package_dir

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
                self._write_meta(cache_key, source, build_dir)
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

    def _write_meta(self, cache_key: str, source: SourceDefinition, package_dir: Path) -> None:
        created_at = datetime.now(UTC)
        meta = {
            "cache_key": cache_key,
            "created_at": created_at.isoformat(),
            "expires_at": (created_at + self.cache_store.ttl).isoformat(),
            "pipeline_version": self.pipeline_version,
            "source_id": source.source_id,
            "source_signature": source.signature(),
            "artifacts": {
                "region_pbf": "region.osm.pbf",
                "mbtiles": "map.mbtiles",
                "graph": "graph.json",
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
                meta=str(package_dir / "meta.json"),
            ),
        )


def build_default_service() -> MapService:
    maps_root = Path(os.getenv("MAPS_ROOT", "/osm_maps"))
    manifest_path = Path(
        os.getenv("SOURCES_MANIFEST", str(maps_root / "sources" / "manifest.json"))
    )
    ttl_hours = int(os.getenv("CACHE_TTL_HOURS", "12"))
    pipeline_version = os.getenv("PIPELINE_VERSION", "v1")
    tilemaker_threads = os.getenv("TILEMAKER_THREADS", str(max(1, min(4, os.cpu_count() or 1))))
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
        os.getenv("TILEMAKER_CONFIG", "/opt/tilemaker/config.json"),
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
        cache_store=CacheStore(maps_root / "cache", ttl=timedelta(hours=ttl_hours)),
        package_builder=CompositeRegionPackageBuilder(
            extractor=OsmiumRegionExtractor(binary=os.getenv("OSMIUM_BINARY", "osmium")),
            graph_builder=GraphBuilder(),
            mbtiles_builder=CommandMbtilesBuilder(
                shlex.split(mbtiles_command) if mbtiles_command else tilemaker_command
            ),
        ),
        pipeline_version=pipeline_version,
    )


def _normalized_bbox(area: MapArea) -> dict[str, str]:
    return {
        "west": f"{area.west:.6f}",
        "south": f"{area.south:.6f}",
        "east": f"{area.east:.6f}",
        "north": f"{area.north:.6f}",
    }


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
