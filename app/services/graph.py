import json
import math
import subprocess
from pathlib import Path
from urllib.parse import unquote

from app.helpers.model import MapArea


class GraphBuilder:
    def __init__(self, binary: str = "osmium", location_index: str = "flex_mem") -> None:
        self.binary = binary
        self.location_index = location_index

    def build(self, input_path: Path, output_path: Path, area: MapArea) -> None:
        node_indexes: dict[int, int] = {}
        nodes: list[list[float]] = []
        edges: list[list[float | int]] = []
        road_types: dict[str, int] = {}

        for way in self._iter_cropped_ways(input_path, area):
            road_type_id = road_types.setdefault(way["highway"], len(road_types))
            oneway = way["oneway"]
            points = way["points"]

            for ref, lat, lon in points:
                if ref not in node_indexes:
                    node_indexes[ref] = len(nodes)
                    nodes.append([round(lat, 6), round(lon, 6)])

            for current, following in zip(points, points[1:]):
                dist = _haversine(current[1], current[2], following[1], following[2])
                if dist <= 0:
                    continue
                edges.append(
                    [
                        node_indexes[current[0]],
                        node_indexes[following[0]],
                        round(dist, 1),
                        road_type_id,
                        oneway,
                    ]
                )

        with output_path.open("w", encoding="utf-8") as handle:
            json.dump(
                {
                    "road_types": _road_type_list(road_types),
                    "nodes": nodes,
                    "edges": edges,
                },
                handle,
                separators=(",", ":"),
            )

    def _iter_cropped_ways(self, input_path: Path, area: MapArea):
        command = [
            self.binary,
            "add-locations-to-ways",
            "--no-progress",
            "--ignore-missing-nodes",
            "--index-type",
            self.location_index,
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
                if not line.startswith("w"):
                    continue

                fields = _parse_opl_fields(line)
                tags = _parse_tags(fields.get("T", ""))
                highway = tags.get("highway")
                node_field = fields.get("N")
                if not highway or not node_field:
                    continue

                points = _parse_way_points(node_field)
                if len(points) < 2:
                    continue

                oneway_value = tags.get("oneway", "").lower()
                if oneway_value == "-1":
                    points.reverse()
                    oneway = 1
                else:
                    oneway = 1 if oneway_value in {"yes", "1", "true"} else 0

                for cropped_points in _crop_way_points(points, area):
                    if len(cropped_points) < 2:
                        continue
                    yield {
                        "highway": highway,
                        "oneway": oneway,
                        "points": cropped_points,
                    }
        finally:
            stderr = process.stderr.read()
            return_code = process.wait()
            if return_code != 0:
                raise subprocess.CalledProcessError(
                    return_code,
                    command,
                    output=None,
                    stderr=stderr,
                )


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
        tags[unquote(key)] = unquote(value)
    return tags


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


def _crop_way_points(
    points: list[tuple[int, float, float]],
    area: MapArea,
) -> list[list[tuple[int, float, float]]]:
    segments: list[list[tuple[int, float, float]]] = []
    inside_start: int | None = None

    for index, point in enumerate(points):
        inside = _point_in_area(point[1], point[2], area)
        if inside and inside_start is None:
            inside_start = index
        if inside_start is None:
            continue

        next_inside = False
        if index + 1 < len(points):
            next_point = points[index + 1]
            next_inside = _point_in_area(next_point[1], next_point[2], area)

        if next_inside:
            continue

        segment_start = max(0, inside_start - 1)
        segment_end = min(len(points), index + 2)
        segment = points[segment_start:segment_end]
        if len(segment) >= 2:
            segments.append(segment)
        inside_start = None

    return segments


def _point_in_area(lat: float, lon: float, area: MapArea) -> bool:
    return area.west <= lon <= area.east and area.south <= lat <= area.north


def _road_type_list(road_types: dict[str, int]) -> list[str]:
    names = [""] * len(road_types)
    for name, index in road_types.items():
        names[index] = name
    return names


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371000.0
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    a = (
        math.sin(d_lat / 2) * math.sin(d_lat / 2)
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(d_lon / 2)
        * math.sin(d_lon / 2)
    )
    return radius * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
