"""Build a small, source-traceable SUMO dataset. Network access is required."""

import argparse
import datetime as dt
import hashlib
import json
import math
import re
import subprocess
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
HEADERS = {"User-Agent": "TrafficForgeDataset/0.1 (research dataset; contact: https://github.com/TaoLbr1993/TrafficForge)", "Accept": "application/json, application/xml, text/xml"}
PLACES = [
    ("train", "Berlin, Germany"),
    ("train", "Paris, France"),
    ("train", "New York, New York, USA"),
    ("train", "Tokyo, Japan"),
    ("dev", "Amsterdam, Netherlands"),
    ("test", "Vienna, Austria"),
]


def request(url, data=None):
    req = urllib.request.Request(url, data=data, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=120) as response:
        return response.read()


def run(command):
    result = subprocess.run([str(value) for value in command], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode:
        raise RuntimeError(f"Command failed: {command}\n{result.stderr}")
    return result


def rel(path):
    return path.relative_to(ROOT).as_posix()


def write_xml(path, element):
    ET.ElementTree(element).write(path, encoding="utf-8", xml_declaration=True)


def remove_local_build_comment(path):
    content = path.read_text(encoding="utf-8")
    content = re.sub(r"\s*<!-- generated on .*?-->\s*", "\n\n", content, count=1, flags=re.DOTALL)
    path.write_text(content, encoding="utf-8", newline="\n")


def build_artificial(netconvert, split, index):
    group = f"{split}-synthetic-{index:02d}"
    folder = DATA / "artifacts" / group
    folder.mkdir(parents=True, exist_ok=True)
    shape = "cross" if index % 2 == 0 else "t_junction"
    # Distinct parameter values across splits avoid identical generated networks.
    length = {"train": 100, "dev": 160, "test": 220}[split] + 10 * (index // 4)
    lanes = 1 + (index % 3)
    speed = 10 + 2 * (index % 4)
    tls = index % 3 == 0
    positions = {"center": (0, 0), "north": (0, length), "south": (0, -length), "east": (length, 0), "west": (-length, 0)}
    arms = ["north", "south", "east"] + (["west"] if shape == "cross" else [])
    nodes = ET.Element("nodes")
    ET.SubElement(nodes, "node", id="center", x="0", y="0", type="traffic_light" if tls else "priority")
    for arm in arms:
        x, y = positions[arm]
        ET.SubElement(nodes, "node", id=arm, x=str(x), y=str(y))
    edges = ET.Element("edges")
    for arm in arms:
        for start, end in [(arm, "center"), ("center", arm)]:
            ET.SubElement(edges, "edge", id=f"{start}_to_{end}", **{"from": start, "to": end, "numLanes": str(lanes), "speed": str(speed)})
    nodes_path, edges_path = folder / "nodes.nod.xml", folder / "edges.edg.xml"
    base_path, edited_path = folder / "network.net.xml", folder / "edited.net.xml"
    write_xml(nodes_path, nodes)
    write_xml(edges_path, edges)
    run([netconvert, "--node-files", nodes_path, "--edge-files", edges_path, "--output-file", base_path])
    remove_local_build_comment(base_path)
    target = "north_to_center"
    new_speed = speed + 3
    patch = ET.Element("edges")
    ET.SubElement(patch, "edge", id=target, speed=str(new_speed))
    patch_path = folder / "edit.edg.xml"
    write_xml(patch_path, patch)
    run([netconvert, "--sumo-net-file", base_path, "--edge-files", patch_path, "--output-file", edited_path])
    remove_local_build_comment(edited_path)
    kind = "十字路口" if shape == "cross" else "T 形路口，朝北、朝南和朝东三条支路"
    light = "设置信号灯" if tls else "设置优先通行路口，不设信号灯"
    synthetic = {
        "id": group, "task": "synthetic", "split": split, "group_id": group,
        "instruction": f"生成一个{kind}，各方向每条道路为 {lanes} 条车道，限速 {speed} 米/秒，各支路长度 {length} 米，中心{light}。",
        "artifacts": {"reference_net": rel(base_path), "nodes": rel(nodes_path), "edges": rel(edges_path)},
        "expected": {"junction": "center", "junction_type": "traffic_light" if tls else "priority", "arms": arms, "lanes_per_directed_edge": lanes, "speed_mps": speed, "arm_length_m": length},
        "quality": "automatic_pilot",
    }
    edit = {
        "id": group + "-edit", "task": "edit", "split": split, "group_id": group,
        "instruction": f"将地图中由北向中心路口行驶的道路限速从 {speed} 米/秒改为 {new_speed} 米/秒，其他道路限速不变。",
        "artifacts": {"input_net": rel(base_path), "reference_net": rel(edited_path), "patch": rel(patch_path)},
        "expected": {"target_edge": target, "old_speed_mps": speed, "new_speed_mps": new_speed, "preserve_other_edge_speeds": True},
        "quality": "automatic_pilot",
    }
    return synthetic, edit


def build_real(netconvert, split, query, index):
    group = f"{split}-place-{index:02d}"
    folder = DATA / "artifacts" / group
    folder.mkdir(parents=True, exist_ok=True)
    cached = folder / "source.json"
    if cached.exists():
        metadata = json.loads(cached.read_text(encoding="utf-8"))
        if metadata["query"] != query:
            raise ValueError(f"Existing source query differs for {group}")
        return real_record(split, group, folder, metadata)
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode({"q": query, "format": "jsonv2", "limit": 5, "accept-language": "en"})
    response = request(url)
    (folder / "nominatim.json").write_bytes(response)
    candidates = json.loads(response)
    if not candidates:
        raise ValueError(f"Nominatim returned no candidates for {query}")
    place = candidates[0]
    if query.split(",")[0].casefold() not in place.get("display_name", "").casefold():
        raise ValueError(f"Ambiguous Nominatim result for {query}: {place.get('display_name')}")
    lat, lon = float(place["lat"]), float(place["lon"])
    radius = 120
    dy = radius / 111320
    dx = dy / math.cos(math.radians(lat))
    west, south, east, north = lon - dx, lat - dy, lon + dx, lat + dy
    bbox = [round(value, 7) for value in (west, south, east, north)]
    overpass = f'[out:xml][timeout:60];way["highway"]({south:.7f},{west:.7f},{north:.7f},{east:.7f})->.roads;(.roads;>;rel(bw.roads)["type"="restriction"];>;);out meta;'
    osm_path = folder / "source.osm.xml"
    failure = None
    for endpoint in ("https://overpass-api.de/api/interpreter", "https://overpass.private.coffee/api/interpreter"):
        try:
            osm = request(endpoint, urllib.parse.urlencode({"data": overpass}).encode())
            break
        except Exception as exc:
            failure = exc
    else:
        raise RuntimeError(f"All Overpass endpoints failed: {failure}")
    osm_root = ET.fromstring(osm)
    roads = osm_root.findall("way")
    if len(roads) < 3:
        raise ValueError(f"Too few roads for {query}: {len(roads)}")
    osm_path.write_bytes(osm)
    (folder / "overpass_query.txt").write_text(overpass + "\n", encoding="utf-8")
    net = folder / "network.net.xml"
    run([netconvert, "--osm-files", osm_path, "--output-file", net, "--keep-edges.by-vclass", "passenger"])
    remove_local_build_comment(net)
    edge_count = len([e for e in ET.parse(net).getroot().findall("edge") if not e.get("function")])
    if edge_count < 3:
        raise ValueError(f"Too few SUMO road edges for {query}: {edge_count}")
    stamp = dt.datetime.now(dt.timezone.utc).isoformat()
    metadata = {"query": query, "nominatim_url": url, "overpass_endpoint": endpoint, "display_name": place["display_name"], "osm_type": place["osm_type"], "osm_id": place["osm_id"], "latitude": lat, "longitude": lon, "bbox_wgs84": bbox, "osm_ways": len(roads), "sumo_edges": edge_count, "retrieved_utc": stamp, "osm_sha256": hashlib.sha256(osm).hexdigest(), "netconvert_options": ["--keep-edges.by-vclass", "passenger"]}
    (folder / "source.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return real_record(split, group, folder, metadata)


def real_record(split, group, folder, metadata):
    query, bbox = metadata["query"], metadata["bbox_wgs84"]
    return {
        "id": group, "task": "real_map", "split": split, "group_id": group,
        "instruction": f"从 OpenStreetMap 提取 {query} 中心附近，矩形范围经度 {bbox[0]} 到 {bbox[2]}、纬度 {bbox[1]} 到 {bbox[3]} 的机动车道路，保留 Overpass 边界查询返回的完整道路。",
        "artifacts": {"reference_net": rel(folder / "network.net.xml"), "osm_snapshot": rel(folder / "source.osm.xml"), "geocoder_response": rel(folder / "nominatim.json"), "source_metadata": rel(folder / "source.json")},
        "expected": {"place_query": query, "osm_type": metadata["osm_type"], "osm_id": metadata["osm_id"], "center_wgs84": [metadata["longitude"], metadata["latitude"]], "bbox_wgs84": bbox, "boundary_rule": "overpass_bbox_complete_ways", "vehicle_class": "passenger"},
        "quality": "automatic_pilot",
    }


def build_circle(netconvert, rectangle):
    metadata = json.loads((ROOT / rectangle["artifacts"]["source_metadata"]).read_text(encoding="utf-8"))
    lat, lon = metadata["latitude"], metadata["longitude"]
    radius = 90
    polygon = []
    for i in range(36):
        angle = 2 * math.pi * i / 36
        polygon.extend([lon + radius * math.cos(angle) / (111320 * math.cos(math.radians(lat))), lat + radius * math.sin(angle) / 111320])
    boundary = ",".join(f"{value:.8f}" for value in polygon)
    folder = (ROOT / rectangle["artifacts"]["reference_net"]).parent
    output = folder / "circle.net.xml"
    run([netconvert, "--sumo-net-file", folder / "network.net.xml", "--keep-edges.in-geo-boundary", boundary, "--output-file", output])
    remove_local_build_comment(output)
    count = len([edge for edge in ET.parse(output).getroot().findall("edge") if not edge.get("function")])
    if count < 3:
        raise ValueError(f"Circular map has too few edges for {rectangle['id']}: {count}")
    sample = dict(rectangle)
    sample["id"] = rectangle["id"] + "-circle"
    sample["instruction"] = f"以 WGS84 坐标纬度 {lat:.7f}、经度 {lon:.7f} 为中心，提取半径约 {radius} 米的机动车路网，按 SUMO 圆形近似边界筛选道路。"
    sample["artifacts"] = dict(rectangle["artifacts"], reference_net=rel(output))
    sample["expected"] = dict(rectangle["expected"], radius_m=radius, circle_polygon_wgs84=polygon, boundary_rule="sumo_36_vertex_polygon_edge_selection")
    return sample


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--netconvert", required=True)
    parser.add_argument("--sumo", required=True)
    parser.add_argument("--resume", action="store_true", help="Resume an incomplete build with no manifest")
    args = parser.parse_args()
    if DATA.exists() and (not args.resume or (DATA / "manifest.json").exists()):
        raise SystemExit("data/ exists; --resume is only allowed for an incomplete build")
    DATA.mkdir(exist_ok=True)
    records = {"train": [], "dev": [], "test": []}
    for split, count in [("train", 12), ("dev", 4), ("test", 4)]:
        for index in range(count):
            records[split].extend(build_artificial(args.netconvert, split, index))
    for index, (split, place) in enumerate(PLACES):
        print(f"Fetching {place}", flush=True)
        rectangle = build_real(args.netconvert, split, place, index)
        records[split].append(rectangle)
        records[split].append(build_circle(args.netconvert, rectangle))
        time.sleep(1.2)
    for split, items in records.items():
        (DATA / f"{split}.jsonl").write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in items), encoding="utf-8")
    manifest = {"version": "0.1.1-pilot", "created_utc": dt.datetime.now(dt.timezone.utc).isoformat(), "netconvert_version": run([args.netconvert, "--version"]).stdout.splitlines()[0], "sumo_version": run([args.sumo, "--version"]).stdout.splitlines()[0], "counts": {split: {task: sum(item["task"] == task for item in items) for task in ("synthetic", "real_map", "edit")} for split, items in records.items()}, "label_status": "automatic_pilot", "license": "Real map data © OpenStreetMap contributors, ODbL"}
    (DATA / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
