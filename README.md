[![pipeline status](https://git.hxgroup.com/hexops/homelander/badges/master/pipeline.svg)](https://git.hxgroup.com/hexops/homelander/-/commits/master)

![Homelander](assets/homelander.png)

# Homelander

Homelander is a regional map packaging API for offline/mobile applications.

It receives a bounding box (`west`, `south`, `east`, `north`), selects the correct South/Central America `.osm.pbf` source, extracts the requested region, generates a vector tile package (`.mbtiles`), builds a road graph (`graph.json`), and caches the result on disk.

The system is designed for repeated regional requests where the same area may be requested multiple times within a short time window.

## What It Solves

This service exists to avoid building map artifacts manually for every requested area.

It solves four operational problems:

- It turns raw Geofabrik `.osm.pbf` extracts into application-ready regional packages.
- It avoids reprocessing the same area over and over by caching builds for a limited TTL.
- It keeps the web API responsive by pushing heavy generation work to a queue worker.
- It exposes job status and streaming progress so clients do not wait blindly for long builds.

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

1. Client submits a bounding box to `POST /api/map`.
2. API validates the request and resolves the matching source extract.
3. API enqueues a background job and returns `202 Accepted` with a `job_id`.
4. Worker extracts the region with `osmium extract`.
5. Worker builds `graph.json` from the regional `.osm.pbf`.
6. Worker generates `map.mbtiles` with `tilemaker --bbox ...`.
7. Package is written to cache and exposed through job status/result.

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
- source id
- source signature
- pipeline version

Each package is stored under the source namespace and expires after the configured TTL.

This keeps repeated requests cheap while allowing the cache to be discarded aggressively.

## API

### Submit a job

`POST /api/map`

Request body:

```json
{
  "west": -49.31,
  "south": -25.49,
  "east": -49.23,
  "north": -25.42
}
```

Response:

```json
{
  "job_id": "b5d3...",
  "status": "queued",
  "cache_key": "9f3c2a8e",
  "source_id": "south-america-latest"
}
```

### Poll job status

`GET /api/map/jobs/{job_id}`

Returns queued/running/succeeded/failed plus final package metadata when available.

### Stream progress for a new job

`POST /api/map/stream`

Returns newline-delimited JSON (`application/x-ndjson`).

### Stream progress for an existing job

`GET /api/map/jobs/{job_id}/stream`

This endpoint is useful when the client first submits a job, then attaches to progress later.

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
curl -X POST http://localhost:3502/api/map \
  -H 'Content-Type: application/json' \
  -d '{
    "west": -46.70,
    "south": -23.62,
    "east": -46.56,
    "north": -23.50
  }'
```

Stream progress:

```bash
curl -N -X POST http://localhost:3502/api/map/stream \
  -H 'Content-Type: application/json' \
  -d '{
    "west": -46.70,
    "south": -23.62,
    "east": -46.56,
    "north": -23.50
  }'
```

## Operational Notes

- Redis is internal to Docker Compose and is not exposed on the host by default.
- The API process does not perform heavy builds inline.
- The worker is the only component responsible for extraction and package generation.
- If the mounted volume is empty or source files are missing, the API cannot resolve requests.
- If a cached package already exists and is still valid, the job can resolve quickly without rebuilding artifacts.

## Test Suite

Run:

```bash
./.venv/bin/python -m unittest discover -s tests -v
```

The test suite covers source resolution, cache behavior, graph generation, job submission/status flow, streaming behavior, and source sync logic.
