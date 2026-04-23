import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from app.helpers.model import MapArea
from app.services.graph import GraphBuilder


OSM_XML = """<?xml version='1.0' encoding='UTF-8'?>
<osm version="0.6" generator="test">
  <node id="1" lat="-25.4300" lon="-49.2700" />
  <node id="2" lat="-25.4310" lon="-49.2710" />
  <node id="3" lat="-25.4320" lon="-49.2720" />
  <node id="4" lat="-25.4315" lon="-49.2690" />
  <way id="10">
    <nd ref="1" />
    <nd ref="2" />
    <nd ref="3" />
    <tag k="highway" v="residential" />
    <tag k="oneway" v="yes" />
  </way>
  <way id="11">
    <nd ref="4" />
    <nd ref="2" />
    <tag k="highway" v="service" />
  </way>
</osm>
"""


class GraphBuilderTests(unittest.TestCase):
    def test_builds_graph_from_region_pbf_using_manual_bbox_crop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            osm_path = root / "input.osm"
            pbf_path = root / "input.osm.pbf"
            graph_path = root / "graph.json"
            osm_path.write_text(OSM_XML, encoding="utf-8")
            subprocess.run(
                ["osmium", "cat", str(osm_path), "-o", str(pbf_path), "--overwrite"],
                check=True,
            )

            GraphBuilder().build(
                pbf_path,
                graph_path,
                MapArea(west=-49.2715, south=-25.4321, east=-49.2685, north=-25.4305),
            )
            graph = json.loads(graph_path.read_text(encoding="utf-8"))

            self.assertEqual(graph["road_types"], ["residential", "service"])
            self.assertEqual(len(graph["nodes"]), 4)
            self.assertEqual(len(graph["edges"]), 3)
            self.assertEqual(graph["edges"][0][4], 1)


if __name__ == "__main__":
    unittest.main()
