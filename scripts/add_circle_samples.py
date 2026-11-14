"""Add circular examples to an existing v0.1 pilot from frozen source maps."""

import argparse
import json
from pathlib import Path

from build_pilot import DATA, build_circle


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--netconvert", required=True)
    args = parser.parse_args()
    manifest_path = DATA / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest["version"] != "0.1.0-pilot":
        raise SystemExit("Expected the original v0.1.0-pilot manifest")
    records = {}
    for split in ("train", "dev", "test"):
        path = DATA / f"{split}.jsonl"
        items = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        if any(item["id"].endswith("-circle") for item in items):
            raise SystemExit(f"Circular samples already exist in {split}")
        records[split] = items + [build_circle(args.netconvert, item) for item in items if item["task"] == "real_map"]
    for split, items in records.items():
        path = DATA / f"{split}.jsonl"
        path.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in items), encoding="utf-8")
        manifest["counts"][split]["real_map"] = sum(item["task"] == "real_map" for item in items)
    manifest["version"] = "0.1.1-pilot"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest["counts"], indent=2))


if __name__ == "__main__":
    main()
