import json
import math
from pathlib import Path

import osmium


class GraphBuilder:
    def __init__(self, location_index: str = "flex_mem") -> None:
        self.location_index = location_index

    def build(self, input_path: Path, output_path: Path) -> None:
        collector = _GraphCollector()
        collector.apply_file(str(input_path), locations=True, idx=self.location_index)
        with output_path.open("w", encoding="utf-8") as handle:
            json.dump(
                {
                    "road_types": collector.road_type_list(),
                    "nodes": collector.nodes,
                    "edges": collector.edges,
                },
                handle,
                separators=(",", ":"),
            )


class _GraphCollector(osmium.SimpleHandler):
    def __init__(self) -> None:
        super().__init__()
        self.node_indexes: dict[int, int] = {}
        self.nodes: list[list[float]] = []
        self.edges: list[list[float | int]] = []
        self.road_types: dict[str, int] = {}

    def way(self, way: osmium.osm.Way) -> None:
        highway = way.tags.get("highway")
        if not highway:
            return

        node_indexes = self.node_indexes
        append_node = self._append_node
        points: list[tuple[int, float, float]] = []
        for node in way.nodes:
            if not node.location.valid():
                return
            points.append((node.ref, node.location.lat, node.location.lon))

        if len(points) < 2:
            return

        oneway_value = way.tags.get("oneway", "").lower()
        if oneway_value == "-1":
            points.reverse()
            oneway = 1
        else:
            oneway = 1 if oneway_value in {"yes", "1", "true"} else 0

        road_type_id = self.road_types.setdefault(highway, len(self.road_types))

        for ref, lat, lon in points:
            if ref not in node_indexes:
                node_indexes[ref] = append_node(lat, lon)

        for current, following in zip(points, points[1:]):
            dist = _haversine(current[1], current[2], following[1], following[2])
            if dist <= 0:
                continue
            self.edges.append(
                [
                    node_indexes[current[0]],
                    node_indexes[following[0]],
                    round(dist, 1),
                    road_type_id,
                    oneway,
                ]
            )

    def _append_node(self, lat: float, lon: float) -> int:
        index = len(self.nodes)
        self.nodes.append([round(lat, 6), round(lon, 6)])
        return index

    def road_type_list(self) -> list[str]:
        names = [""] * len(self.road_types)
        for name, index in self.road_types.items():
            names[index] = name
        return names


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371000.0
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(d_lon / 2) ** 2
    )
    return radius * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
