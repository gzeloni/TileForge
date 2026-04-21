[![pipeline status](https://git.hxgroup.com/hexops/homelander/badges/master/pipeline.svg)](https://git.hxgroup.com/hexops/homelander/-/commits/master)

![Homelander](assets/homelander.png)

# Homelander

Homelander is a regional map packaging API for offline/mobile applications.

It receives a bounding box (`west`, `south`, `east`, `north`) plus `minZoom`, selects the correct South/Central America `.osm.pbf` source, extracts the requested region, generates a vector tile package (`.mbtiles`), builds a road graph (`graph.json`), and caches the result on disk.

The system is designed for repeated regional requests where the same area may be requested multiple times within a short time window.

## What It Solves

This service exists to avoid building map artifacts manually for every requested area.

It solves four operational problems:

- It turns raw Geofabrik `.osm.pbf` extracts into application-ready regional packages.
- It avoids reprocessing the same area over and over by caching builds for a limited TTL.
- It keeps the web API responsive by pushing heavy generation work to a queue worker.
- It exposes stable package status, artifact download endpoints, and streaming progress so clients do not wait blindly for long builds.

## Output

Each successful job produces a regional package containing:

- `region.osm.pbf`
- `map.mbtiles`
- `graph.json`
- `meta.json`

These files are stored under the mounted maps volume, grouped by source and cache key.

## Architecture

The service is split into three runtime components:

- `homelander`: FastAPI web API
- `homelander-worker`: RQ worker that performs heavy map builds
- `redis`: queue backend and job/progress state store

Main tooling:

- `osmium` for source inspection and regional extraction
- `tilemaker` for `.mbtiles` generation
- Python `osmium` bindings for graph extraction
- `RQ + Redis` for job execution and progress/state tracking

## High-Level Flow

1. Client submits a bounding box and `minZoom` to `POST /api/map/packages`.
2. API validates the request and resolves the matching source extract.
3. API enqueues or reuses a package build and returns `202 Accepted` with a stable `package_id`.
4. Worker extracts the region with `osmium extract`.
5. Worker builds `graph.json` from the regional `.osm.pbf`.
6. Worker generates `map.mbtiles` with `tilemaker --bbox ...` and a generated config whose `settings.minzoom` matches the request.
7. Package is written to cache and exposed through package status and artifact endpoints.

## Source Management

Source files are expected in the mounted maps volume, for example:

```text
/osm_maps/
  south-america-latest.osm.pbf
  central-america-latest.osm.pbf
```

The service uses `/osm_maps/sources/manifest.json` as the source registry.

If the manifest does not exist, the container bootstraps it automatically by scanning local `.osm.pbf` files and extracting their coverage bounds. The generated manifest is persisted in the mounted volume so subsequent boots do not repeat that expensive discovery step.

## Cache Model

Cache entries are keyed by:

- normalized bounding box
- requested `minZoom`
- source id
- source signature
- pipeline version

Each package is stored under the source namespace and expires after the configured TTL.

This keeps repeated requests cheap while allowing the cache to be discarded aggressively.

## API

All endpoints require:

```http
Authorization: Bearer <API_KEY>
```

### Create or reuse a package

`POST /api/map/packages`

Request body:

```json
{
  "west": -46.70,
  "south": -23.62,
  "east": -46.56,
  "north": -23.50,
  "minZoom": 5
}
```

Response:

```json
{
  "package_id": "9f3c2a8e",
  "status": "queued",
  "source_id": "south-america-latest",
  "job_id": "b5d3...",
  "expires_at": null,
  "artifacts": {
    "region_pbf": "/api/map/packages/9f3c2a8e/artifacts/region_pbf",
    "mbtiles": "/api/map/packages/9f3c2a8e/artifacts/mbtiles",
    "graph": "/api/map/packages/9f3c2a8e/artifacts/graph",
    "meta": "/api/map/packages/9f3c2a8e/artifacts/meta"
  }
}
```

`package_id` is the stable identifier clients should keep.

### Poll package status

`GET /api/map/packages/{package_id}`

Returns:

- `queued`
- `building`
- `ready`
- `failed`

plus artifact URLs when the package is ready.

### Download artifacts

- `GET /api/map/packages/{package_id}/artifacts/mbtiles`
- `GET /api/map/packages/{package_id}/artifacts/graph`
- `GET /api/map/packages/{package_id}/artifacts/meta`
- `GET /api/map/packages/{package_id}/artifacts/region_pbf`

These are the public download URLs. Clients should not attempt to access internal server paths such as `/osm_maps/cache/...`.

### Stream progress for a new job

`POST /api/map/stream`

Returns newline-delimited JSON (`application/x-ndjson`). This remains available for submit-and-stream flows.

### Stream progress for an existing package

`GET /api/map/packages/{package_id}/stream`

This endpoint is useful when the client first submits a package request, then attaches to progress later.

### Legacy compatibility

The older job-based endpoints still exist for compatibility:

- `POST /api/map`
- `GET /api/map/jobs/{job_id}`
- `GET /api/map/jobs/{job_id}/stream`
- `GET /api/map/jobs/{job_id}/artifacts/{artifact_name}`

They should not be the primary integration target for new clients.

## Progress Model

Progress events are stored in Redis and streamed as NDJSON.

Typical event types:

- `job`
- `progress`
- `result`
- `error`

`progress` events may include live subprocess output from `osmium` or `tilemaker`.

## Local Development

Build and run:

```bash
docker compose build --no-cache
docker compose up -d redis homelander homelander-worker
```

Check logs:

```bash
docker compose logs -f homelander
docker compose logs -f homelander-worker
```

Submit a job:

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

Download MBTiles:

```bash
curl -L \
  -H 'Authorization: Bearer YOUR_API_KEY' \
  http://localhost:3502/api/map/packages/PACKAGE_ID/artifacts/mbtiles \
  -o map.mbtiles
```

Stream progress:

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

## Operational Notes

- Redis is internal to Docker Compose and is not exposed on the host by default.
- The API process does not perform heavy builds inline.
- The worker is the only component responsible for extraction and package generation.
- If the mounted volume is empty or source files are missing, the API cannot resolve requests.
- If a cached package already exists and is still valid, the package can resolve quickly without rebuilding artifacts.
- `minZoom` changes the package identity. The same bbox with different `minZoom` values generates different packages.
- The public integration contract is package-based, not filesystem-based.

## Test Suite

Run:

```bash
./.venv/bin/python -m unittest discover -s tests -v
```

The test suite covers source resolution, cache behavior, package submission/status flow, package artifact downloads, graph generation, streaming behavior, `minZoom` handling, and source sync logic.
