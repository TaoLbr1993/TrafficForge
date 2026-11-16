"""Numeric scale of a successful extraction, independent of rendered road coverage."""
import json
import math
from decimal import Decimal, InvalidOperation
from pathlib import Path

from judge import digest


DEFAULT_TOLERANCE = 0.10


def tolerance(value):
    alpha = float(value)
    if not math.isfinite(alpha) or not 0 <= alpha < 1:
        raise ValueError("range tolerance must be in [0, 1)")
    return alpha


def positive(value):
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    return number if number.is_finite() and number > 0 else None


def radius_task(sample):
    return sample["task"] == "real_map" and "radius_m" in sample["expected"]


def numeric_range(sample, prediction, manifest_dir, alpha):
    """Read evaluator-side adapter evidence. Never take a number from the instruction."""
    requested = positive(sample["expected"]["radius_m"])
    if requested is None:
        raise ValueError(f"invalid reference radius: {sample['id']}")
    a = Decimal(str(tolerance(alpha)))
    lo, hi = requested * (1 - a), requested * (1 + a)
    result = {"status":"unverified", "requested_m":float(requested), "alpha":float(a),
              "inclusive_interval_m":[float(lo), float(hi)]}
    if not prediction or prediction.get("status", "ok") != "ok" or not prediction.get("net"):
        return {**result, "reason":(prediction or {}).get("status", "missing_output")}
    net = (manifest_dir / prediction["net"]).resolve()
    if not net.is_file():
        return {**result, "reason":"missing_network"}
    try:
        if "range_metadata" in prediction:
            # Optional execution receipt for other baselines, produced by their tool wrapper.
            meta = json.loads((manifest_dir / prediction["range_metadata"]).read_text("utf-8-sig"))
            if meta["id"] != sample["id"] or meta["net_sha256"] != digest(net) or meta["status"] != "ok":
                return {**result, "reason":"unmatched_execution_receipt"}
            metres = positive(meta["executed_radius_m"])
            source = "executed_radius_m"
            shape = meta["query_shape"]
            if shape not in ("circle", "bbox", "unknown"):
                return {**result, "reason":"invalid_query_shape"}
        else:
            trace_path = (manifest_dir / prediction["trace"] if "trace" in prediction
                          else net.parent / "trace.json")
            if not trace_path.is_file():
                return {**result, "reason":"missing_execution_trace"}
            trace = json.loads(trace_path.read_text("utf-8-sig"))
            if trace["id"] != sample["id"] or trace["status"] != "ok" or trace["task"] != "real_map":
                return {**result, "reason":"unmatched_execution_trace"}
            for step in reversed(trace["history"]):
                obs = step.get("observation")
                if step.get("action") != "BUILD_NET" or not isinstance(obs, dict) or obs.get("status") != "ok":
                    continue
                if not obs.get("net") or (trace_path.parent / obs["net"]).resolve() != net:
                    continue
                params = step.get("params") or {}
                if params.get("type", "build_from_realworld") != "build_from_realworld":
                    return {**result, "reason":"not_a_realworld_build"}
                miles = positive(params.get("distance_miles"))
                metres = miles * Decimal("1609.344") if miles is not None else None
                break
            else:
                return {**result, "reason":"no_successful_build_for_output"}
            source, shape = "executed_distance_miles", "bbox"
    except (KeyError, TypeError, AttributeError, ValueError, OSError) as exc:
        return {**result, "reason":"invalid_execution_record", "error_type":type(exc).__name__}
    if metres is None or not math.isfinite(float(metres)):
        return {**result, "reason":"invalid_executed_distance"}
    return {**result, "status":"pass" if lo <= metres <= hi else "fail",
            "measured_m":float(metres), "source":source, "query_shape":shape}


def range_summary(checks):
    passed = sum(check["status"] == "pass" for check in checks)
    failed = sum(check["status"] == "fail" for check in checks)
    return {"requested":len(checks), "passed":passed, "failed":failed,
            "unverified":len(checks)-passed-failed,
            "pass_rate_among_verified":passed/(passed+failed) if passed+failed else None}
