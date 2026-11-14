"""Validate pilot dataset provenance, labels, split groups, and SUMO loading."""

import argparse
import collections
import hashlib
import json
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def external_edges(path):
    root = ET.parse(path).getroot()
    return {edge.get("id"): edge for edge in root.findall("edge") if not edge.get("function")}


def lane_speeds(edge):
    return [float(lane.get("speed")) for lane in edge.findall("lane")]


def check(condition, message):
    if not condition:
        raise ValueError(message)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sumo", required=True)
    args = parser.parse_args()
    seen_ids = set()
    groups = {}
    counts = collections.Counter()
    networks = set()
    for split in ("train", "dev", "test"):
        for number, line in enumerate((ROOT / "data" / f"{split}.jsonl").read_text(encoding="utf-8").splitlines(), 1):
            sample = json.loads(line)
            identifier = sample["id"]
            check(identifier not in seen_ids, f"Duplicate ID {identifier}")
            seen_ids.add(identifier)
            check(sample["split"] == split, f"Wrong split: {identifier}")
            group = sample["group_id"]
            check(groups.setdefault(group, split) == split, f"Group leakage: {group}")
            counts[(split, sample["task"])] += 1
            for name, value in sample["artifacts"].items():
                path = (ROOT / value).resolve()
                check(path.is_relative_to(ROOT) and path.is_file(), f"Missing {identifier} {name}: {value}")
            net = ROOT / sample["artifacts"]["reference_net"]
            networks.add(net)
            expected = sample["expected"]
            edges = external_edges(net)
            if sample["task"] == "synthetic":
                for arm in expected["arms"]:
                    for identifier_edge in (f"{arm}_to_center", f"center_to_{arm}"):
                        check(identifier_edge in edges, f"{identifier}: missing {identifier_edge}")
                        lanes = edges[identifier_edge].findall("lane")
                        check(len(lanes) == expected["lanes_per_directed_edge"], f"{identifier}: lane count")
                        check(all(abs(float(lane.get("speed")) - expected["speed_mps"]) < 0.01 for lane in lanes), f"{identifier}: speed")
                junction = ET.parse(net).getroot().find("junction[@id='center']")
                check(junction is not None and junction.get("type") == expected["junction_type"], f"{identifier}: junction type")
            elif sample["task"] == "edit":
                old = external_edges(ROOT / sample["artifacts"]["input_net"])
                networks.add(ROOT / sample["artifacts"]["input_net"])
                check(old.keys() == edges.keys(), f"{identifier}: unexpected changed edge set")
                target = expected["target_edge"]
                check(all(abs(s - expected["old_speed_mps"]) < 0.01 for s in lane_speeds(old[target])), f"{identifier}: original speed")
                check(all(abs(s - expected["new_speed_mps"]) < 0.01 for s in lane_speeds(edges[target])), f"{identifier}: target speed")
                for edge_id in old:
                    if edge_id != target:
                        check(lane_speeds(old[edge_id]) == lane_speeds(edges[edge_id]), f"{identifier}: changed {edge_id}")
            elif sample["task"] == "real_map":
                metadata = json.loads((ROOT / sample["artifacts"]["source_metadata"]).read_text(encoding="utf-8"))
                candidates = json.loads((ROOT / sample["artifacts"]["geocoder_response"]).read_text(encoding="utf-8"))
                check(any(c["osm_type"] == expected["osm_type"] and c["osm_id"] == expected["osm_id"] for c in candidates), f"{identifier}: missing OSM place")
                osm = (ROOT / sample["artifacts"]["osm_snapshot"]).read_bytes()
                check(hashlib.sha256(osm).hexdigest() == metadata["osm_sha256"], f"{identifier}: snapshot hash mismatch")
                check(len(ET.fromstring(osm).findall("way")) >= 3 and len(edges) >= 3, f"{identifier}: insufficient road data")
            else:
                raise ValueError(f"Unknown task in line {number}: {sample['task']}")
    manifest = json.loads((ROOT / "data" / "manifest.json").read_text(encoding="utf-8"))
    for split, tasks in manifest["counts"].items():
        for task, count in tasks.items():
            check(counts[(split, task)] == count, f"Manifest mismatch {split}/{task}")
    for net in sorted(networks):
        result = subprocess.run([args.sumo, "-n", str(net), "-b", "0", "-e", "1", "--no-step-log", "true"], capture_output=True, text=True, encoding="utf-8", errors="replace")
        check(result.returncode == 0, f"SUMO failed to load {net}: {result.stderr}")
    print(f"Validated {len(seen_ids)} samples, {len(groups)} source groups, and {len(networks)} SUMO networks.")
    for (split, task), count in sorted(counts.items()):
        print(f"  {split:5} {task:10} {count}")


if __name__ == "__main__":
    main()
