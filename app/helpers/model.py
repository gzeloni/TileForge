from pydantic import BaseModel, ConfigDict, model_validator


class MapArea(BaseModel):
    model_config = ConfigDict(frozen=True)

    west: float
    south: float
    east: float
    north: float

    @model_validator(mode="after")
    def validate_bounds(self) -> "MapArea":
        if not -180 <= self.west <= 180:
            raise ValueError("west must be between -180 and 180")
        if not -180 <= self.east <= 180:
            raise ValueError("east must be between -180 and 180")
        if not -90 <= self.south <= 90:
            raise ValueError("south must be between -90 and 90")
        if not -90 <= self.north <= 90:
            raise ValueError("north must be between -90 and 90")
        if self.west >= self.east:
            raise ValueError("west must be smaller than east")
        if self.south >= self.north:
            raise ValueError("south must be smaller than north")
        return self


class ArtifactPaths(BaseModel):
    region_pbf: str
    mbtiles: str
    graph: str
    meta: str


class MapPackageResponse(BaseModel):
    status: str
    cached: bool
    cache_key: str
    source_id: str
    expires_at: str
    artifacts: ArtifactPaths


class MapJobAcceptedResponse(BaseModel):
    job_id: str
    status: str
    cache_key: str
    source_id: str


class MapJobStatusResponse(BaseModel):
    job_id: str
    status: str
    cache_key: str
    source_id: str
    error: str | None = None
    result: MapPackageResponse | None = None
