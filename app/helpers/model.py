from pydantic import BaseModel, ConfigDict, Field, model_validator


class MapArea(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True)

    west: float
    south: float
    east: float
    north: float
    min_zoom: int = Field(default=0, alias="minZoom")

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
        if not 0 <= self.min_zoom <= 22:
            raise ValueError("minZoom must be between 0 and 22")
        return self


class ArtifactPaths(BaseModel):
    region_pbf: str
    mbtiles: str
    graph: str
    addresses: str
    meta: str


class MapPackageAcceptedResponse(BaseModel):
    package_id: str
    status: str
    source_id: str
    job_id: str | None = None
    expires_at: str | None = None
    artifacts: ArtifactPaths


class MapPackageStatusResponse(BaseModel):
    package_id: str
    status: str
    source_id: str
    job_id: str | None = None
    error: str | None = None
    expires_at: str | None = None
    artifacts: ArtifactPaths | None = None


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
