"""Evaluate predictions from any TrafficForge baseline. See README.md for the commands."""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

from judge import SCALE, digest, grade, json_write, validated
from maps import Network, evidence
from range_check import DEFAULT_TOLERANCE, numeric_range, radius_task, range_summary, tolerance

ROOT = Path(__file__).resolve().parents[1]
TASKS = ("synthetic", "real_map", "edit")


def lines(path):
    return [json.loads(s) for s in path.read_text("utf-8-sig").splitlines() if s.strip()]


def records(path):
    indexed = {}
    for row in lines(path):
        name = row["id"]
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", name) or name in indexed:
            raise ValueError(f"invalid or duplicate id: {name!r}")
        indexed[name] = row
    return indexed


def write_lines(path, rows):
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf8")


def sumo_binary(value):
    candidates = [value] if value else [shutil.which("sumo"), ROOT / ".deps/sumo/bin/sumo.exe",
                                        ROOT / ".deps/sumo/bin/sumo"]
    for candidate in candidates:
        if candidate:
            resolved = shutil.which(str(candidate)) or str(Path(candidate).resolve())
            if Path(resolved).is_file():
                return resolved
    raise ValueError("SUMO executable unavailable; set --sumo")


def gate(score_reason):
    return {"state":"failed", "score":0, "reason":score_reason}


def is_timeout(row):
    # Legacy runs stored timeouts as failed/0. Interpret them using the current policy.
    return row.get("state") == "timeout" or row.get("reason") in ("timeout", "sumo_timeout")


def is_unscored(row):
    return is_timeout(row) or row.get("state") == "infrastructure_error"


def prepare_case(sample, submission, args, binary, folder):
    if submission is None:
        return gate("missing_prediction")
    status = submission.get("status", "ok")
    if status == "timeout":
        return {"state":"timeout", "reason":"timeout"}
    if status == "infrastructure_error":
        return {"state":"infrastructure_error", "reason":submission.get("reason", "infrastructure_error")}
    if status in ("unsupported", "failed"):
        return gate(status)
    if status != "ok" or not isinstance(submission.get("net"), str) or not submission["net"].strip():
        raise ValueError(f"invalid submission row for {sample['id']}")
    path = (args.predictions.parent / submission["net"]).resolve()
    if not path.is_file():
        return gate("missing_network")
    try:
        candidate = Network(path)
    except (ET.ParseError, ValueError, KeyError, IndexError) as exc:
        return {**gate("invalid_network"), "detail":str(exc)[:300]}
    try:
        loaded = subprocess.run([binary, "-n", str(path), "-b", "0", "-e", "1", "--no-step-log", "true"],
                                cwd=folder, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=args.timeout,
                                encoding="utf8", errors="replace")
    except subprocess.TimeoutExpired:
        return {"state":"timeout", "reason":"sumo_timeout"}
    if loaded.returncode:
        (folder / "load.log").write_text(loaded.stdout, encoding="utf8")
        return gate("sumo_load_failed")
    task = sample["task"]
    source = None
    reference_path = None
    if task in ("real_map", "edit"):
        field = "reference_net" if task == "real_map" else "input_net"
        reference_path = ROOT / sample["artifacts"][field]
        source = Network(reference_path)
    packet = {"task":task, "instruction":sample["instruction"], "expected":sample["expected"],
              "evidence":evidence(sample, candidate, source, folder),
              "images":(["reference.png"] if source else []) + ["candidate.png"]}
    extra = {}
    if radius_task(sample):
        check = numeric_range(sample, submission, args.predictions.parent,
                              getattr(args, "range_tolerance", DEFAULT_TOLERANCE))
        packet["evidence"]["range_check"] = check
        extra["range_check"] = check
    json_write(folder / "packet.json", packet)
    return {"state":"ready", "net_sha256":digest(path), "packet_sha256":digest(folder / "packet.json"),
            "reference_sha256": digest(reference_path) if reference_path else None, **extra}


def prepare(args):
    data = getattr(args, "data_dir", ROOT / "data") / (args.split + ".jsonl")
    dataset, predictions = records(data), records(args.predictions)
    ids_file = getattr(args, "ids_file", None)
    if ids_file:
        selected = {s.strip() for s in ids_file.read_text("utf-8-sig").splitlines() if s.strip()}
        if not selected or selected - set(dataset):
            raise ValueError("IDs file is empty or contains IDs outside the selected split")
        dataset = {k: v for k, v in dataset.items() if k in selected}
        if selected - set(predictions):
            raise ValueError("selected IDs must all have predictions")
    unknown = set(predictions) - set(dataset)
    if unknown:
        raise ValueError(f"predictions outside selected split: {sorted(unknown)}")
    if args.run.exists():
        raise ValueError("run directory exists; choose a new directory")
    binary = sumo_binary(args.sumo)
    version = subprocess.run([binary, "--version"], capture_output=True, text=True, errors="replace",
                             check=True).stdout.splitlines()[0]
    args.run.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=args.run.parent) as temp:
        stage = Path(temp) / "run"
        items = []
        for identifier, sample in dataset.items():
            folder = stage / "cases" / identifier
            folder.mkdir(parents=True)
            result = prepare_case(sample, predictions.get(identifier), args, binary, folder)
            if radius_task(sample) and "range_check" not in result:
                result["range_check"] = {"status":"unverified", "reason":result["reason"],
                                         "requested_m":sample["expected"]["radius_m"]}
            items.append({"id":identifier, "task":sample["task"], "group_id":sample["group_id"], **result})
        write_lines(stage / "items.jsonl", items)
        json_write(stage / "run.json", {"split":args.split,"split_sha256":digest(data),
                                        "predictions_sha256":digest(args.predictions),
                                        "sumo":version,"scale":SCALE,"samples":len(items),
                                        "range_tolerance":getattr(args, "range_tolerance", DEFAULT_TOLERANCE),
                                        **({"ids_sha256":digest(ids_file)} if ids_file else {})})
        stage.rename(args.run)
    print(f"Prepared {len(items)} cases in {args.run}:", dict(Counter(x["state"] for x in items)))


def rating(run, row):
    if is_unscored(row):
        return None
    if row["state"] == "failed":
        return {"score":0,"reason":row["reason"]}
    path = run / "cases" / row["id"] / "rating.json"
    return validated(json.loads(path.read_text("utf8"))) if path.exists() else None


def report(run, items):
    results = []
    for row in items:
        value = rating(run, row)
        if value:
            reason = value["reason"]
        elif is_unscored(row):
            reason = row.get("reason", "timeout")
        else:
            reason = "awaiting_judge"
        results.append({**row, "state":"timeout" if is_timeout(row) else row["state"],
                        "score":value["score"] if value else None, "reason":reason})
    def summarize(rows):
        excluded = sum(is_timeout(row) for row in rows)
        infrastructure = sum(row["state"] == "infrastructure_error" for row in rows)
        eligible = [row for row in rows if not is_unscored(row)]
        scores = Counter(row["score"] for row in eligible)
        complete = bool(eligible) and scores[None] == 0
        return {"total":len(rows), "timeout":excluded, "infrastructure_error":infrastructure, "eligible":len(eligible), "scored":len(eligible)-scores[None],
                "pending":scores[None], "score_0":scores[0], "score_1":scores[1], "score_2":scores[2],
                "full_success_rate":scores[2]/len(eligible) if complete else None,
                "mean_score":(scores[1]+2*scores[2])/len(eligible) if complete else None}
    tasks = {t:summarize([row for row in results if row["task"] == t]) for t in TASKS}
    rates = [v["full_success_rate"] for v in tasks.values()]
    summary = {"overall":summarize(results),"by_task":tasks,
               "macro_task_success_rate":sum(rates)/len(rates) if None not in rates else None,
               "failure_reasons":dict(Counter(x["reason"] for x in results if x["state"]=="failed" and not is_timeout(x))),
               "timeout_reasons":dict(Counter(x["reason"] for x in results if is_timeout(x))),
               "infrastructure_error_reasons":dict(Counter(x["reason"] for x in results if x["state"] == "infrastructure_error"))}
    if any("range_check" in row for row in results):
        summary["numeric_range"] = range_summary([r["range_check"] for r in results if "range_check" in r])
    write_lines(run / "results.jsonl", results)
    json_write(run / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


def common_subset(runs):
    systems = {}
    split_hashes = set()
    tolerances = set()
    for run in runs:
        name = str(run.resolve())
        systems[name] = records(run / "items.jsonl")
        metadata = json.loads((run / "run.json").read_text("utf8"))
        split_hashes.add(metadata["split_sha256"])
        tolerances.add(metadata.get("range_tolerance"))
    if len(systems) < 2 or len(split_hashes) != 1:
        raise ValueError("compare requires two distinct runs from the same split revision")
    if len({frozenset(rows) for rows in systems.values()}) != 1:
        raise ValueError("compare requires runs containing the same sample IDs")
    if len(tolerances) != 1 and any("range_check" in row for rows in systems.values() for row in rows.values()):
        raise ValueError("compare requires the same range scoring policy and tolerance")
    included = [i for i in next(iter(systems.values()))
                if all(system[i].get("reason") != "unsupported" and not is_unscored(system[i]) for system in systems.values())]
    comparison = {}
    for name, rows in systems.items():
        scores = {i:rating(Path(name), rows[i]) for i in included}
        result = {}
        for task in (*TASKS, "overall"):
            subset = [v for i,v in scores.items() if task == "overall" or rows[i]["task"] == task]
            pending = subset.count(None)
            result[task] = {"n":len(subset), "pending":pending, "full_success_rate":
                            sum(v["score"] == 2 for v in subset)/len(subset) if subset and not pending else None}
        comparison[name] = result
    return {"common_ids":included, "runs":comparison}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    init = sub.add_parser("prepare", help="read a split, load networks and render blind evidence")
    init.add_argument("--split", choices=["train","dev","test"], required=True)
    init.add_argument("--data-dir", type=Path, default=ROOT / "data")
    init.add_argument("--predictions", type=Path, required=True)
    init.add_argument("--run", type=Path, required=True)
    init.add_argument("--sumo")
    init.add_argument("--timeout", type=int, default=45)
    init.add_argument("--range-tolerance", type=tolerance, default=DEFAULT_TOLERANCE, metavar="A")
    init.add_argument("--ids-file", type=Path, help="Evaluate only these split IDs; requires a prediction for each")
    cloud = sub.add_parser("judge", help="call a cloud vision model and resume any unrated cases")
    cloud.add_argument("--run", type=Path, required=True)
    cloud.add_argument("--model", default=os.getenv("TRAFFICFORGE_JUDGE_MODEL"))
    cloud.add_argument("--base-url", default=os.getenv("TRAFFICFORGE_JUDGE_BASE_URL","https://api.openai.com/v1"))
    cloud.add_argument("--key-env", default="TRAFFICFORGE_JUDGE_API_KEY")
    cloud.add_argument("--timeout", type=int, default=120)
    cloud.add_argument("--retries", type=int, default=2)
    cloud.add_argument("--limit", type=int, help="submit at most this many pending cases this invocation")
    rep = sub.add_parser("report", help="summarize scores; incomplete scores are null")
    rep.add_argument("--run", type=Path, required=True)
    ranges = sub.add_parser("range-report", help="score submitted radius tasks from execution records without images or cloud calls")
    ranges.add_argument("--split", choices=["train","dev","test"], required=True)
    ranges.add_argument("--data-dir", type=Path, default=ROOT / "data")
    ranges.add_argument("--predictions", type=Path, required=True)
    ranges.add_argument("--range-tolerance", type=tolerance, default=DEFAULT_TOLERANCE, metavar="A")
    ranges.add_argument("--out", type=Path, help="optional new JSON report (also printed to stdout)")
    comp = sub.add_parser("compare", help="report a shared subset excluding unsupported and timed-out cases")
    comp.add_argument("--run", action="append", type=Path, required=True)
    comp.add_argument("--out", type=Path, required=True)
    smoke = sub.add_parser("reference-predictions", help="local self-check ONLY; references are not baseline predictions")
    smoke.add_argument("--split", choices=["train","dev","test"], required=True)
    smoke.add_argument("--data-dir", type=Path, default=ROOT / "data")
    smoke.add_argument("--out", type=Path, required=True)
    args = p.parse_args()
    if getattr(args, "timeout", 1) <= 0 or getattr(args, "retries", 0) < 0:
        p.error("timeout must be positive and retries nonnegative")
    if args.command == "judge" and args.limit is not None and args.limit <= 0:
        p.error("--limit must be positive")
    if args.command == "reference-predictions":
        if args.out.exists():
            raise ValueError("predictions file already exists")
        args.out.parent.mkdir(parents=True, exist_ok=True)
        write_lines(args.out, [{"id":row["id"],"net":str((ROOT / row["artifacts"]["reference_net"]).resolve())}
                               for row in records(args.data_dir / (args.split+".jsonl")).values()])
        return 0
    if args.command == "prepare":
        prepare(args)
        return 0
    if args.command == "range-report":
        dataset = records(args.data_dir / (args.split + ".jsonl"))
        predictions = records(args.predictions)
        if predictions.keys() - dataset.keys():
            raise ValueError("predictions contain IDs outside the selected split")
        checks = {i:numeric_range(s, predictions[i], args.predictions.parent, args.range_tolerance)
                  for i,s in dataset.items() if i in predictions and radius_task(s)}
        result = {"split":args.split, "predictions_sha256":digest(args.predictions),
                  "range_tolerance":args.range_tolerance, **range_summary(list(checks.values())), "cases":checks}
        if args.out:
            if args.out.exists():
                raise ValueError("range report already exists")
            args.out.parent.mkdir(parents=True, exist_ok=True)
            json_write(args.out, result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.command == "compare":
        if args.out.exists():
            raise ValueError("comparison output file already exists")
        result = common_subset(args.run)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        json_write(args.out, result)
        print(f"Compared {len(result['common_ids'])} shared cases.")
        return 0
    items = lines(args.run / "items.jsonl")
    if args.command == "report":
        report(args.run, items)
        return 0
    failures = grade(args, args.run, items)
    report(args.run, items)
    return int(bool(failures))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError, subprocess.SubprocessError, KeyError) as exc:
        sys.exit(str(exc))
