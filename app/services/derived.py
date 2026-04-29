"""Derived source lifecycle services."""

import json
import os
import sqlite3
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from redis import Redis
from rq import Queue
from rq.registry import StartedJobRegistry

from app.helpers.model import MapArea
from app.services.maps import (
    OsmiumRegionExtractor,
    SourceDefinition,
    SourceRegistry,
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def manifest_lock_path(manifest_path: Path) -> Path:
    return manifest_path.with_name(f"{manifest_path.name}.lock")


def derived_index_path(maps_root: Path) -> Path:
    return Path(
        os.getenv(
            "DERIVED_INDEX_PATH",
            str(maps_root / "cache" / "derived_sources.sqlite3"),
        )
    )


@contextmanager
def manifest_lock(manifest_path: Path):
    import fcntl

    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = manifest_lock_path(manifest_path)
    with lock_path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@dataclass(frozen=True)
class DerivedRegionDefinition:
    region_id: str
    source_id: str
    parent_source_id: str
    west: float
    south: float
    east: float
    north: float
    path: Path
    kind: str
    request_threshold: int = 0
    pinned: bool = False
    priority: int = 10

    def contains(self, area: MapArea) -> bool:
        return (
            self.west <= area.west
            and self.south <= area.south
            and self.east >= area.east
            and self.north >= area.north
        )

    def footprint(self) -> float:
        return (self.east - self.west) * (self.north - self.south)

    def area(self) -> MapArea:
        return MapArea(
            west=self.west,
            south=self.south,
            east=self.east,
            north=self.north,
        )


class DerivedRegionCatalog:
    def __init__(self, regions: list[DerivedRegionDefinition]) -> None:
        self.regions = regions

    @classmethod
    def from_env(cls, maps_root: Path) -> "DerivedRegionCatalog":
        resolver = RegionBoundsResolver(
            cache_path=derived_index_path(maps_root),
            ttl=timedelta(hours=float(os.getenv("REGION_LOOKUP_CACHE_TTL_HOURS", "168"))),
            base_url=os.getenv(
                "REGION_LOOKUP_URL",
                "https://nominatim.openstreetmap.org/search",
            ),
            user_agent=os.getenv(
                "REGION_LOOKUP_USER_AGENT",
                "homelander-region-resolver/1.0",
            ),
            timeout=float(os.getenv("REGION_LOOKUP_TIMEOUT_SECONDS", "15")),
        )
        default_regions = _load_region_definitions(
            os.getenv("DERIVED_DEFAULT_REGIONS", "[]"),
            maps_root=maps_root,
            default_kind="default",
            default_pinned=True,
            resolver=resolver,
        )
        adaptive_regions = _load_region_definitions(
            os.getenv("DERIVED_ADAPTIVE_REGIONS", "[]"),
            maps_root=maps_root,
            default_kind="adaptive",
            default_pinned=False,
            resolver=resolver,
        )
        return cls(default_regions + adaptive_regions)

    def is_empty(self) -> bool:
        return not self.regions

    def defaults(self) -> list[DerivedRegionDefinition]:
        return [region for region in self.regions if region.kind == "default" or region.pinned]

    def adaptive(self) -> list[DerivedRegionDefinition]:
        return [region for region in self.regions if region.kind == "adaptive" and not region.pinned]

    def get(self, region_id: str) -> DerivedRegionDefinition:
        for region in self.regions:
            if region.region_id == region_id:
                return region
        raise KeyError(region_id)

    def match(self, area: MapArea) -> DerivedRegionDefinition | None:
        matches = [region for region in self.regions if region.contains(area)]
        if not matches:
            return None
        matches.sort(key=lambda region: (region.footprint(), region.priority, region.region_id))
        return matches[0]


def _load_region_definitions(
    raw_payload: str,
    maps_root: Path,
    default_kind: str,
    default_pinned: bool,
    resolver=None,
) -> list[DerivedRegionDefinition]:
    if not raw_payload.strip():
        return []
    payload = json.loads(raw_payload)
    regions: list[DerivedRegionDefinition] = []
    for item in payload:
        bounds = resolver.resolve(item) if resolver is not None else item.get("bounds", item)
        raw_path = item.get(
            "path",
            str(maps_root / "derived" / f"{item['source_id']}.osm.pbf"),
        )
        path = Path(raw_path)
        if not path.is_absolute():
            path = maps_root / path
        regions.append(
            DerivedRegionDefinition(
                region_id=item["region_id"],
                source_id=item["source_id"],
                parent_source_id=item["parent_source_id"],
                west=bounds["west"],
                south=bounds["south"],
                east=bounds["east"],
                north=bounds["north"],
                path=path,
                kind=item.get("kind", default_kind),
                request_threshold=int(item.get("request_threshold", 0)),
                pinned=bool(item.get("pinned", default_pinned)),
                priority=int(item.get("priority", 10)),
            )
        )
    return regions


class RegionBoundsResolver:
    def __init__(
        self,
        cache_path: Path,
        ttl: timedelta,
        base_url: str,
        user_agent: str,
        timeout: float = 15.0,
    ) -> None:
        self.cache_path = cache_path
        self.ttl = ttl
        self.base_url = base_url
        self.user_agent = user_agent
        self.timeout = timeout
        self._initialized = False
        self._lock = threading.Lock()

    def resolve(self, item: dict) -> dict[str, float]:
        bounds = item.get("bounds")
        if bounds is None and {"west", "south", "east", "north"} <= item.keys():
            bounds = item
        if bounds is not None:
            return {
                "west": float(bounds["west"]),
                "south": float(bounds["south"]),
                "east": float(bounds["east"]),
                "north": float(bounds["north"]),
            }

        query = item.get("query")
        if not query:
            raise ValueError(
                f"Region {item.get('region_id', item.get('source_id', '<unknown>'))} must define bounds or query"
            )
        country_codes = item.get("country_codes")
        cache_key = self._cache_key(query, country_codes)
        cached = self._read_cache(cache_key)
        if cached is not None:
            return cached

        bounds = self._fetch_bounds(query, country_codes)
        self._write_cache(cache_key, query, country_codes, bounds)
        return bounds

    def _fetch_bounds(self, query: str, country_codes: str | None) -> dict[str, float]:
        params = {
            "q": query,
            "format": "jsonv2",
            "limit": 1,
        }
        if country_codes:
            params["countrycodes"] = country_codes
        response = httpx.get(
            self.base_url,
            params=params,
            headers={"User-Agent": self.user_agent},
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        if not payload:
            raise ValueError(f"Could not resolve bounds for query: {query}")
        bounding_box = payload[0].get("boundingbox")
        if not bounding_box or len(bounding_box) != 4:
            raise ValueError(f"Lookup response does not contain boundingbox for query: {query}")
        return {
            "west": float(bounding_box[2]),
            "south": float(bounding_box[0]),
            "east": float(bounding_box[3]),
            "north": float(bounding_box[1]),
        }

    def _read_cache(self, cache_key: str) -> dict[str, float] | None:
        self._initialize()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT west, south, east, north, fetched_at
                FROM region_bounds_cache
                WHERE cache_key = ?
                """,
                (cache_key,),
            ).fetchone()
        if row is None:
            return None
        fetched_at = datetime.fromisoformat(row["fetched_at"])
        if _utcnow() - fetched_at >= self.ttl:
            return None
        return {
            "west": float(row["west"]),
            "south": float(row["south"]),
            "east": float(row["east"]),
            "north": float(row["north"]),
        }

    def _write_cache(
        self,
        cache_key: str,
        query: str,
        country_codes: str | None,
        bounds: dict[str, float],
    ) -> None:
        self._initialize()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO region_bounds_cache (
                    cache_key,
                    query,
                    country_codes,
                    west,
                    south,
                    east,
                    north,
                    fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    query = excluded.query,
                    country_codes = excluded.country_codes,
                    west = excluded.west,
                    south = excluded.south,
                    east = excluded.east,
                    north = excluded.north,
                    fetched_at = excluded.fetched_at
                """,
                (
                    cache_key,
                    query,
                    country_codes,
                    bounds["west"],
                    bounds["south"],
                    bounds["east"],
                    bounds["north"],
                    _utcnow().isoformat(),
                ),
            )

    def _cache_key(self, query: str, country_codes: str | None) -> str:
        return json.dumps(
            {"query": query, "country_codes": country_codes or ""},
            sort_keys=True,
        )

    def _initialize(self) -> None:
        with self._lock:
            if self._initialized:
                return
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS region_bounds_cache (
                        cache_key TEXT PRIMARY KEY,
                        query TEXT NOT NULL,
                        country_codes TEXT,
                        west REAL NOT NULL,
                        south REAL NOT NULL,
                        east REAL NOT NULL,
                        north REAL NOT NULL,
                        fetched_at TEXT NOT NULL
                    )
                    """
                )
            self._initialized = True

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.cache_path)
        connection.row_factory = sqlite3.Row
        return connection


class DerivedSourceIndex:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._initialized = False
        self._lock = threading.Lock()

    def initialize(self) -> None:
        with self._lock:
            if self._initialized:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as connection:
                connection.executescript(
                    """
                    PRAGMA journal_mode=WAL;
                    PRAGMA busy_timeout=5000;
                    CREATE TABLE IF NOT EXISTS region_stats (
                        region_id TEXT PRIMARY KEY,
                        parent_source_id TEXT NOT NULL,
                        request_count INTEGER NOT NULL DEFAULT 0,
                        first_seen_at TEXT NOT NULL,
                        last_used_at TEXT NOT NULL,
                        derived_source_id TEXT
                    );
                    CREATE TABLE IF NOT EXISTS derived_sources (
                        source_id TEXT PRIMARY KEY,
                        region_id TEXT NOT NULL,
                        parent_source_id TEXT NOT NULL,
                        path TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        status TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        last_used_at TEXT NOT NULL,
                        last_built_at TEXT NOT NULL,
                        request_count INTEGER NOT NULL DEFAULT 0,
                        source_signature TEXT NOT NULL,
                        pinned INTEGER NOT NULL DEFAULT 0
                    );
                    """
                )
            self._initialized = True

    def record_request(self, region: DerivedRegionDefinition) -> None:
        self.initialize()
        now = _utcnow().isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO region_stats (
                    region_id,
                    parent_source_id,
                    request_count,
                    first_seen_at,
                    last_used_at,
                    derived_source_id
                ) VALUES (?, ?, 1, ?, ?, NULL)
                ON CONFLICT(region_id) DO UPDATE SET
                    request_count = request_count + 1,
                    last_used_at = excluded.last_used_at
                """,
                (
                    region.region_id,
                    region.parent_source_id,
                    now,
                    now,
                ),
            )

    def touch_derived_source(self, source: SourceDefinition) -> None:
        if source.kind == "base" or not source.region_id:
            return
        self.initialize()
        now = _utcnow().isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO derived_sources (
                    source_id,
                    region_id,
                    parent_source_id,
                    path,
                    kind,
                    status,
                    created_at,
                    last_used_at,
                    last_built_at,
                    request_count,
                    source_signature,
                    pinned
                ) VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?, 1, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    last_used_at = excluded.last_used_at,
                    request_count = derived_sources.request_count + 1,
                    status = 'active',
                    source_signature = excluded.source_signature
                """,
                (
                    source.source_id,
                    source.region_id,
                    source.parent_source_id or "",
                    str(source.path),
                    source.kind,
                    now,
                    now,
                    now,
                    source.signature(),
                    1 if source.pinned else 0,
                ),
            )
            connection.execute(
                """
                UPDATE region_stats
                SET derived_source_id = ?, last_used_at = ?
                WHERE region_id = ?
                """,
                (source.source_id, now, source.region_id),
            )

    def register_derived_source(
        self,
        region: DerivedRegionDefinition,
        source_signature: str,
    ) -> None:
        self.initialize()
        now = _utcnow().isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO derived_sources (
                    source_id,
                    region_id,
                    parent_source_id,
                    path,
                    kind,
                    status,
                    created_at,
                    last_used_at,
                    last_built_at,
                    request_count,
                    source_signature,
                    pinned
                ) VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?, 0, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    path = excluded.path,
                    kind = excluded.kind,
                    status = 'active',
                    last_built_at = excluded.last_built_at,
                    source_signature = excluded.source_signature,
                    pinned = excluded.pinned
                """,
                (
                    region.source_id,
                    region.region_id,
                    region.parent_source_id,
                    str(region.path),
                    region.kind,
                    now,
                    now,
                    now,
                    source_signature,
                    1 if region.pinned else 0,
                ),
            )
            connection.execute(
                """
                INSERT INTO region_stats (
                    region_id,
                    parent_source_id,
                    request_count,
                    first_seen_at,
                    last_used_at,
                    derived_source_id
                ) VALUES (?, ?, 0, ?, ?, ?)
                ON CONFLICT(region_id) DO UPDATE SET
                    derived_source_id = excluded.derived_source_id
                """,
                (
                    region.region_id,
                    region.parent_source_id,
                    now,
                    now,
                    region.source_id,
                ),
            )

    def list_promotable_regions(self, catalog: DerivedRegionCatalog) -> list[DerivedRegionDefinition]:
        self.initialize()
        promoted = []
        with self._connect() as connection:
            for region in catalog.adaptive():
                row = connection.execute(
                    """
                    SELECT request_count, derived_source_id
                    FROM region_stats
                    WHERE region_id = ?
                    """,
                    (region.region_id,),
                ).fetchone()
                if row is None:
                    continue
                if row["derived_source_id"]:
                    continue
                if row["request_count"] >= region.request_threshold > 0:
                    promoted.append(region)
        promoted.sort(key=lambda region: (region.request_threshold, region.priority, region.region_id))
        return promoted

    def list_stale_sources(self, idle_ttl: timedelta) -> list[sqlite3.Row]:
        self.initialize()
        cutoff = (_utcnow() - idle_ttl).isoformat()
        with self._connect() as connection:
            return connection.execute(
                """
                SELECT source_id, region_id, path
                FROM derived_sources
                WHERE pinned = 0
                  AND status = 'active'
                  AND last_used_at < ?
                ORDER BY last_used_at ASC
                """,
                (cutoff,),
            ).fetchall()

    def mark_deleted(self, source_id: str, region_id: str) -> None:
        self.initialize()
        with self._connect() as connection:
            connection.execute(
                "UPDATE derived_sources SET status = 'deleted' WHERE source_id = ?",
                (source_id,),
            )
            connection.execute(
                """
                UPDATE region_stats
                SET derived_source_id = NULL
                WHERE region_id = ?
                """,
                (region_id,),
            )

    def last_request_at(self) -> datetime | None:
        self.initialize()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT MAX(last_used_at) AS value FROM region_stats"
            ).fetchone()
        if row is None or not row["value"]:
            return None
        return datetime.fromisoformat(row["value"])

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection


class DerivedSourceManager:
    def __init__(
        self,
        maps_root: Path,
        manifest_path: Path,
        index: DerivedSourceIndex,
        catalog: DerivedRegionCatalog,
        extractor=None,
        idle_ttl: timedelta | None = None,
        redis_url: str | None = None,
        job_queue_name: str = "homelander-maps",
        precache_queue_name: str = "homelander-precache",
        precache_idle_minutes: int = 15,
    ) -> None:
        self.maps_root = maps_root
        self.manifest_path = manifest_path
        self.index = index
        self.catalog = catalog
        self.extractor = extractor or OsmiumRegionExtractor(
            binary=os.getenv("OSMIUM_BINARY", "osmium")
        )
        self.idle_ttl = idle_ttl or timedelta(hours=24)
        self.redis_url = redis_url
        self.job_queue_name = job_queue_name
        self.precache_queue_name = precache_queue_name
        self.precache_idle_minutes = max(1, precache_idle_minutes)

    def ensure_defaults(self) -> list[str]:
        created: list[str] = []
        for region in self.catalog.defaults():
            if self.ensure_region(region):
                created.append(region.source_id)
        return created

    def record_request(self, area: MapArea, source: SourceDefinition) -> None:
        if self.catalog.is_empty():
            return
        region = self.catalog.match(area)
        if region is not None:
            self.index.record_request(region)
        self.index.touch_derived_source(source)

    def sync_once(self) -> dict[str, list[str]]:
        idle = self._can_precache()
        created_defaults = self.ensure_defaults() if idle else []
        enqueued = self.enqueue_hot_regions() if idle else []
        deleted = self.cleanup_stale_sources()
        return {
            "created_defaults": created_defaults,
            "enqueued": enqueued,
            "deleted": deleted,
        }

    def enqueue_hot_regions(self) -> list[str]:
        if not self._can_precache():
            return []
        if not self.redis_url:
            return []
        queue = self._queue(self.precache_queue_name)
        enqueued: list[str] = []
        for region in self.index.list_promotable_regions(self.catalog):
            queue.enqueue(
                "app.services.derived.run_derived_region_job",
                region.region_id,
                job_id=f"derived-{region.region_id}",
            )
            enqueued.append(region.region_id)
            break
        return enqueued

    def cleanup_stale_sources(self) -> list[str]:
        deleted: list[str] = []
        for row in self.index.list_stale_sources(self.idle_ttl):
            self.remove_region_source(row["source_id"], row["region_id"], Path(row["path"]))
            deleted.append(row["source_id"])
        return deleted

    def ensure_region_by_id(self, region_id: str) -> bool:
        return self.ensure_region(self.catalog.get(region_id))

    def ensure_region(self, region: DerivedRegionDefinition) -> bool:
        self.index.initialize()
        with manifest_lock(self.manifest_path):
            registry = self._load_registry()
            parent = self._resolve_parent_source(registry, region.parent_source_id)
            current = next(
                (source for source in registry.sources if source.source_id == region.source_id),
                None,
            )
            parent_signature = parent.signature()
            if (
                current is not None
                and current.path.exists()
                and current.parent_source_signature == parent_signature
            ):
                self.index.register_derived_source(region, current.signature())
                return False

            build_dir = Path(tempfile.mkdtemp(prefix=f"{region.source_id}-", dir=str(self.maps_root)))
            output_path = build_dir / region.path.name
            try:
                build_dir.mkdir(parents=True, exist_ok=True)
                region.path.parent.mkdir(parents=True, exist_ok=True)
                self.extractor.extract(parent.path, region.area(), output_path)
                output_path.replace(region.path)
                derived_source = SourceDefinition(
                    source_id=region.source_id,
                    path=region.path,
                    west=region.west,
                    south=region.south,
                    east=region.east,
                    north=region.north,
                    priority=region.priority,
                    signature_override=None,
                    download_url=None,
                    kind=region.kind,
                    region_id=region.region_id,
                    parent_source_id=region.parent_source_id,
                    parent_source_signature=parent_signature,
                    pinned=region.pinned,
                )
                payload = self._upsert_manifest_source(registry, derived_source)
                self.manifest_path.write_text(json.dumps(payload), encoding="utf-8")
                self.index.register_derived_source(region, derived_source.signature())
                return True
            finally:
                if build_dir.exists():
                    for item in build_dir.iterdir():
                        if item.exists():
                            item.unlink()
                    build_dir.rmdir()

    def remove_region_source(self, source_id: str, region_id: str, path: Path) -> None:
        with manifest_lock(self.manifest_path):
            registry = self._load_registry()
            payload = {
                "sources": [
                    item
                    for item in registry.to_manifest_payload()["sources"]
                    if item["source_id"] != source_id
                ]
            }
            self.manifest_path.write_text(json.dumps(payload), encoding="utf-8")
        if path.exists():
            path.unlink()
        self.index.mark_deleted(source_id, region_id)

    def _can_precache(self) -> bool:
        if not self.redis_url:
            return False
        last_request = self.index.last_request_at()
        if last_request is not None:
            if _utcnow() - last_request < timedelta(minutes=self.precache_idle_minutes):
                return False
        main_queue = self._queue(self.job_queue_name)
        precache_queue = self._queue(self.precache_queue_name)
        if len(main_queue) > 0 or len(precache_queue) > 0:
            return False
        if len(StartedJobRegistry(self.job_queue_name, main_queue.connection).get_job_ids()) > 0:
            return False
        if len(StartedJobRegistry(self.precache_queue_name, precache_queue.connection).get_job_ids()) > 0:
            return False
        return True

    def _load_registry(self) -> SourceRegistry:
        if self.manifest_path.exists():
            return SourceRegistry.from_file(self.manifest_path)
        raise FileNotFoundError(f"Manifest not found: {self.manifest_path}")

    def _resolve_parent_source(
        self,
        registry: SourceRegistry,
        parent_source_id: str,
    ) -> SourceDefinition:
        for source in registry.sources:
            if source.source_id == parent_source_id:
                return source
        raise ValueError(f"Parent source not found: {parent_source_id}")

    def _upsert_manifest_source(
        self,
        registry: SourceRegistry,
        source: SourceDefinition,
    ) -> dict:
        updated = [item for item in registry.sources if item.source_id != source.source_id]
        updated.append(source)
        updated.sort(key=lambda item: (item.footprint(), item.priority, item.source_id))
        return SourceRegistry(updated).to_manifest_payload()

    def _queue(self, name: str) -> Queue:
        assert self.redis_url is not None
        return Queue(name=name, connection=Redis.from_url(self.redis_url, decode_responses=True))


class ScheduledDerivedSourceSync:
    def __init__(
        self,
        manager: DerivedSourceManager,
        interval_seconds: float = 900.0,
        run_on_start: bool = True,
    ) -> None:
        self.manager = manager
        self.interval_seconds = interval_seconds
        self.run_on_start = run_on_start
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _run(self) -> None:
        next_delay = 0.0 if self.run_on_start else self.interval_seconds
        while not self._stop_event.wait(next_delay):
            try:
                self.manager.sync_once()
            except Exception as exc:
                print(f"derived source sync failed: {exc}", flush=True)
            next_delay = self.interval_seconds


def derived_source_manager() -> DerivedSourceManager | None:
    maps_root = Path(os.getenv("MAPS_ROOT", "/osm_maps"))
    manifest_path = Path(
        os.getenv("SOURCES_MANIFEST", str(maps_root / "sources" / "manifest.json"))
    )
    catalog = DerivedRegionCatalog.from_env(maps_root)
    if catalog.is_empty():
        return None

    index_path = derived_index_path(maps_root)
    redis_url = os.getenv("REDIS_URL")
    manager = DerivedSourceManager(
        maps_root=maps_root,
        manifest_path=manifest_path,
        index=DerivedSourceIndex(index_path),
        catalog=catalog,
        idle_ttl=timedelta(hours=float(os.getenv("DERIVED_SOURCE_IDLE_TTL_HOURS", "24"))),
        redis_url=redis_url,
        job_queue_name=os.getenv("RQ_QUEUE_NAME", "homelander-maps"),
        precache_queue_name=os.getenv("RQ_PRECACHE_QUEUE_NAME", "homelander-precache"),
        precache_idle_minutes=int(os.getenv("DERIVED_PRECACHE_IDLE_MINUTES", "15")),
    )
    return manager


def source_derivation_sync() -> ScheduledDerivedSourceSync | None:
    manager = derived_source_manager()
    if manager is None:
        return None
    return ScheduledDerivedSourceSync(
        manager=manager,
        interval_seconds=max(60.0, float(os.getenv("DERIVED_SYNC_INTERVAL_MINUTES", "15")) * 60),
        run_on_start=os.getenv("DERIVED_SYNC_ON_STARTUP", "1").lower() not in {"0", "false", "no"},
    )


def ensure_default_derived_sources() -> list[str]:
    manager = derived_source_manager()
    if manager is None:
        return []
    return manager.ensure_defaults()


def ensure_default_derived_sources_safe() -> list[str]:
    try:
        return ensure_default_derived_sources()
    except Exception as exc:
        print(f"warning: failed to ensure default derived sources: {exc}", flush=True)
        return []


def run_derived_region_job(region_id: str) -> dict[str, str]:
    manager = derived_source_manager()
    if manager is None:
        raise RuntimeError("derived source manager is not configured")
    created = manager.ensure_region_by_id(region_id)
    return {
        "region_id": region_id,
        "status": "created" if created else "ready",
    }
