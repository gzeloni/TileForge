# TileForge

TileForge is an on-demand, high-performance map packaging service and API for offline and mobile applications. Given a geographic bounding box and a `minZoom`, it resolves the best matching OpenStreetMap source extract, cuts a regional `.osm.pbf`, generates a vector tile package (`.mbtiles`), builds a road graph (`graph.json`), and caches identical requests for reuse.

The service is built for repeated regional requests where the same area may be requested multiple times over a short period. Instead of forcing clients to manage heavy GIS tooling, long-running jobs, or filesystem artifacts, TileForge exposes a package-oriented RESTful API with stable identifiers, status polling, artifact downloads, and progress streaming.

## Overview

TileForge solves a practical packaging problem:

* Transform raw `.osm.pbf` source extracts into application-ready regional artifacts.
* Keep the API responsive by offloading heavy processing to background workers.
* Avoid rebuilding identical requests unnecessarily through cache reuse and TTL-based expiration.
* Provide a stable contract for clients that need package status, progress, and downloadable outputs.

Each successful package contains:

* `region.osm.pbf`
* `map.mbtiles`
* `graph.json`
* `meta.json`

## Runtime Architecture

The default deployment is composed of three services:

* `tileforge`: FastAPI application exposing the HTTP API
* `tileforge-worker`: RQ worker responsible for extraction and package generation
* `redis`: Queue backend plus job/progress state store

Core tooling used by the pipeline:

* `osmium-tool` for source inspection and regional extraction
* Subprocess calls to `osmium-tool` for graph extraction and clipping
* `tilemaker` for MBTiles generation
* `RQ` and `Redis` for background execution and streaming state

High-level request flow:

1. A client submits a bounding box and `minZoom` to `POST /api/map/packages`.
2. The API validates the request and resolves the smallest source extract that fully covers the area.
3. The API computes a package identity and either reuses an active build, returns a cached package, or enqueues a new build.
4. The worker extracts the regional `.osm.pbf`, builds `graph.json`, and generates `map.mbtiles`.
5. Artifacts are persisted under the mounted maps volume and exposed through package endpoints.

## Repository Layout

```text
.
├── app/
│   ├── app.py
│   ├── api.py
│   ├── helpers/
│   ├── routes/
│   └── services/
├── tests/
├── Dockerfile
├── docker-compose.yaml
├── docker-entrypoint.sh
└── requirements.txt

```

Main code areas:

* `app/routes/`: HTTP endpoints and request/response orchestration
* `app/services/maps.py`: Source resolution, cache management, and package build pipeline
* `app/services/jobs.py`: Queue-backed job orchestration and package/job state
* `app/services/sync.py`: Source synchronization and manifest refresh logic
* `tests/`: Unit and integration-style coverage for package flow, cache behavior, graph generation, and source sync

## Storage Model

TileForge expects a writable maps root mounted into the containers. By default, that root is `/osm_maps`.

Typical layout:

```text
/osm_maps/
├── cache/
│   └── <source_id>/
│       └── <cache_key>__<normalized_bbox_slug>/
│           ├── region.osm.pbf
│           ├── map.mbtiles
│           ├── graph.json
│           └── meta.json
└── sources/
    └── manifest.json

```

Source files may live anywhere under `/osm_maps` as long as they are referenced by the manifest or discoverable during bootstrap.

If `/osm_maps/sources/manifest.json` does not exist, the container entrypoint bootstraps it by scanning local `.osm.pbf` files, inspecting their bounds with `osmium`, and persisting the result. This avoids repeating source discovery on every boot.

## Source Resolution And Synchronization

The source manifest is the registry TileForge uses to resolve incoming requests. Each source definition includes:

* `source_id`
* `path`
* Geographic bounds
* Optional priority
* Optional signature override
* Optional `download_url`

When multiple sources cover the same area, TileForge selects the smallest matching footprint. This allows a more specific regional extract to win over a broader continental one.

If source synchronization is enabled, the API process also runs a background scheduler that:

* Checks remote source fingerprints via HTTP `HEAD`
* Downloads updated extracts when the remote state changes
* Refreshes source signatures in the manifest after a successful download

This keeps source data current without requiring a separate sync service.

## Cache Semantics

Package identity is based on:

* Exact bounding box
* Requested `minZoom`
* Source ID
* Source signature
* Pipeline version

Cached packages expire after the configured TTL. Once expired, they are no longer considered ready and may be rebuilt on demand.

`minZoom` is part of the package identity. Two requests for the same bounds but different `minZoom` values produce different packages. Any request with different bounds produces a new package.

## API Contract

All routes require a bearer token:

```http
Authorization: Bearer <API_KEY>

```

Request payload validation:

* `west` and `east` must be between `-180` and `180`
* `south` and `north` must be between `-90` and `90`
* `west < east`
* `south < north`
* `minZoom` must be between `0` and `22`

### Package Endpoints

`POST /api/map/packages`

Creates or reuses a package build and returns `202 Accepted`.

Example request:

```json
{
  "west": -46.70,
  "south": -23.62,
  "east": -46.56,
  "north": -23.50,
  "minZoom": 5
}

```

Example response:

```json
{
  "package_id": "9f3c2a8e",
  "status": "queued",
  "source_id": "south-america-latest",
  "job_id": "b5d3c7d0-...",
  "expires_at": null,
  "artifacts": {
    "region_pbf": "/api/map/packages/9f3c2a8e/artifacts/region_pbf",
    "mbtiles": "/api/map/packages/9f3c2a8e/artifacts/mbtiles",
    "graph": "/api/map/packages/9f3c2a8e/artifacts/graph",
    "addresses": "/api/map/packages/9f3c2a8e/artifacts/addresses",
    "meta": "/api/map/packages/9f3c2a8e/artifacts/meta"
  }
}

```

`GET /api/map/packages/{package_id}`

Returns the current package state. Possible statuses:

* `queued`
* `building`
* `ready`
* `failed`

`GET /api/map/packages/{package_id}/artifacts/{artifact_name}`

Supported artifacts:

* `region_pbf`
* `mbtiles`
* `graph`
* `addresses`
* `meta`

`GET /api/map/packages/{package_id}/stream`

Streams package progress as NDJSON (`application/x-ndjson`).

### Legacy Job Endpoints

The older job-based contract is still available for compatibility:

* `POST /api/map`
* `GET /api/map/jobs/{job_id}`
* `GET /api/map/jobs/{job_id}/stream`
* `GET /api/map/jobs/{job_id}/artifacts/{artifact_name}`
* `GET /api/map/jobs/{job_id}/artifact/{artifact_name}`

New integrations should prefer the package-based endpoints.

### Progress Events

Progress is emitted as NDJSON. Typical event types are:

* `job`
* `progress`
* `result`
* `error`

`progress` events may include subprocess output from `osmium` or `tilemaker`, which is useful for long-running build visibility.

### Error Behavior

Common response classes:

* `401` for missing or invalid API keys
* `404` for unknown packages, jobs, or artifacts
* `409` when an artifact is requested before the package is ready
* `422` for invalid map bounds or `minZoom`
* `500` when required local source data is missing
* `503` when the build backend cannot accept work

## Reverse Proxy Note

The FastAPI app is configured with `root_path=/tileforge` for proxy-aware deployments. In local Docker Compose usage, requests are sent directly to `/api/map/...` on port `3502`. If TileForge is mounted behind a reverse proxy path prefix, generated URLs and upstream routing may use `/tileforge/api/map/...`.

## Configuration

The container runtime expects a `.env` file. The most relevant settings are:

| Variable | Default | Purpose |
| --- | --- | --- |
| `API_KEY` | none | Required bearer token for every API request |
| `MAPS_ROOT` | `/osm_maps` | Root directory for sources, cache, and generated artifacts |
| `SOURCES_MANIFEST` | `${MAPS_ROOT}/sources/manifest.json` | Source registry path |
| `REDIS_URL` | `redis://redis:6379/0` | Redis connection used by the API and worker |
| `RQ_QUEUE_NAME` | `tileforge-maps` | Queue consumed by the worker |
| `JOB_TTL_SECONDS` | `43200` | Retention for job snapshots and progress events |
| `CACHE_TTL_HOURS` | `12` | Package cache TTL |
| `PIPELINE_VERSION` | `v2-smart-extract` | Version token that invalidates old cache entries when changed |
| `BUILD_SCRATCH_ROOT` | system temp dir | Temporary build directory root |
| `PIPELINE_PARALLEL_TASKS` | `2` | Internal build parallelism |
| `OSMIUM_BINARY` | `osmium` | Path to the `osmium` executable |
| `TILEMAKER_BINARY` | `tilemaker` | Path to the `tilemaker` executable |
| `TILEMAKER_CONFIG` | `/opt/tilemaker/config.json` | Base Tilemaker config used when `MBTILES_COMMAND` is not set |
| `TILEMAKER_PROCESS` | `/opt/tilemaker/process.lua` | Tilemaker process Lua file |
| `TILEMAKER_STORE` | `/tmp/tilemaker-store` | Tilemaker intermediate store path |
| `TILEMAKER_THREADS` | auto | Tilemaker worker thread count |
| `MBTILES_COMMAND` | unset | Full custom command template for MBTiles generation |
| `ENABLE_SOURCE_SYNC` | `1` | Enable background source synchronization |
| `SOURCE_SYNC_INTERVAL_HOURS` | `24` | Source sync interval |
| `SOURCE_SYNC_ON_STARTUP` | `1` | Run source sync on process startup |
| `SOURCE_SYNC_TIMEOUT_SECONDS` | `120` | HTTP timeout for remote source checks and downloads |
| `SOURCE_DOWNLOAD_BASE_URL` | `[https://download.geofabrik.de](https://download.geofabrik.de)` | Base URL used when default download URLs are inferred |

Example `.env`:

```env
API_KEY=change-me
REDIS_URL=redis://redis:6379/0
RQ_QUEUE_NAME=tileforge-maps
MAPS_ROOT=/osm_maps
CACHE_TTL_HOURS=12
ENABLE_SOURCE_SYNC=1
SOURCE_SYNC_INTERVAL_HOURS=24

```

## Running With Docker Compose

The repository includes a production-style `docker-compose.yaml` with:

* `tileforge` exposed on `3502`
* `tileforge-worker` consuming the same image
* `redis` kept internal to the Compose network
* A shared maps volume mounted at `/osm_maps`

Build and start the stack:

```bash
docker compose build --no-cache
docker compose up -d redis tileforge tileforge-worker

```

Inspect logs:

```bash
docker compose logs -f tileforge
docker compose logs -f tileforge-worker

```

Stop the stack:

```bash
docker compose down

```

## Local Usage Examples

Create or reuse a package:

```bash
curl -X POST http://localhost:3502/api/map/packages \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer YOUR_API_KEY' \
  -d '{
    "west": -46.70,
    "south": -23.62,
    "east": -46.56,
    "north": -23.50,
    "minZoom": 5
  }'

```

Poll package status:

```bash
curl -H 'Authorization: Bearer YOUR_API_KEY' \
  http://localhost:3502/api/map/packages/PACKAGE_ID

```

Download the MBTiles artifact:

```bash
curl -L \
  -H 'Authorization: Bearer YOUR_API_KEY' \
  http://localhost:3502/api/map/packages/PACKAGE_ID/artifacts/mbtiles \
  -o map.mbtiles

```

Stream package progress from submission time:

```bash
curl -N -X POST http://localhost:3502/api/map/stream \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer YOUR_API_KEY' \
  -d '{
    "west": -46.70,
    "south": -23.62,
    "east": -46.56,
    "north": -23.50,
    "minZoom": 5
  }'

```

Attach to progress later:

```bash
curl -N \
  -H 'Authorization: Bearer YOUR_API_KEY' \
  http://localhost:3502/api/map/packages/PACKAGE_ID/stream

```

## Development Notes

Important operational characteristics:

* Heavy processing is performed only by the worker, not inline in the API process.
* If the maps volume is empty or missing source files, requests cannot be resolved.
* Package downloads must go through the API rather than direct filesystem access.
* Redis state is ephemeral unless your Redis deployment is configured for persistence.
* Cache reuse is intentional, but only for identical requests while they remain valid.

The Docker image:

* Builds `tilemaker` from source during the image build
* Installs `osmium-tool` in the runtime image
* Prepares `/osm_maps`, `/osm_maps/cache`, and `/osm_maps/sources` at container startup

## Testing

Run the test suite with:

```bash
./.venv/bin/python -m unittest discover -s tests -v

```

Current coverage includes:

* Source resolution and manifest bootstrap
* Cache behavior and package reuse
* Package submission, status polling, and artifact downloads
* Graph generation from regional extracts with manual bbox cropping
* Streaming behavior
* `minZoom` handling
* Source synchronization logic
