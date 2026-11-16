"""Numeric extraction-scale tests; no cloud or model access required."""
import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from evaluate import ROOT, lines, prepare, records, report, write_lines
from judge import digest, grade, json_write
from range_check import numeric_range, radius_task, tolerance


class RangeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name)
        data = records(ROOT / "data/dev.jsonl")
        self.sample = next(s for s in data.values() if radius_task(s))
        self.net = ROOT / self.sample["artifacts"]["reference_net"]
        self.row = {"id":self.sample["id"], "net":str(self.net), "trace":"trace.json"}
        self.trace = {"id":self.row["id"], "task":"real_map", "status":"ok", "history":[{
            "action":"BUILD_NET", "params":{"type":"build_from_realworld","distance_miles":0.056},
            "observation":{"status":"ok","net":str(self.net)}}]}

    def check(self):
        json_write(self.folder / "trace.json", self.trace)
        return numeric_range(self.sample, self.row, self.folder, 0.1)

    def test_executed_miles_and_provenance(self):
        result = self.check()
        self.assertEqual(result["status"], "pass")
        self.assertAlmostEqual(result["measured_m"], 90.123264)
        self.assertEqual(result["query_shape"], "bbox")
        step = self.trace["history"][0]
        step["observation"]["status"] = "error"
        self.assertEqual(self.check()["status"], "unverified")
        step["observation"].update(status="ok", net="other.net.xml")
        self.assertEqual(self.check()["status"], "unverified")
        step["observation"]["net"] = str(self.net)
        self.trace["id"] += "-another-case"
        self.assertEqual(self.check()["status"], "unverified")

    def test_closed_interval_and_missing_receipt(self):
        meta = {"id":self.row["id"], "status":"ok", "net_sha256":digest(self.net), "query_shape":"circle"}
        self.row["range_metadata"] = "receipt.json"
        for number, verdict in [(81,"pass"),(99,"pass"),(80.999,"fail"),(99.001,"fail"),("NaN","unverified")]:
            json_write(self.folder / "receipt.json", dict(meta, executed_radius_m=number))
            self.assertEqual(self.check()["status"],verdict)
        json_write(self.folder / "receipt.json", dict(meta, net_sha256="wrong", executed_radius_m=90))
        self.assertEqual(self.check()["status"],"unverified")
        for a in ("nan",1,-0.1):
            with self.assertRaises(ValueError):
                tolerance(a)
        self.assertEqual(tolerance(0),0)

    def test_numeric_result_applies_to_overall_grade(self):
        for i, (miles, other_score, final) in enumerate([(0.112,2,1),(0.056,2,2),(0.056,0,0)]):
            self.trace["history"][0]["params"]["distance_miles"] = miles
            self.check()
            pred = self.folder / "pred.jsonl"
            ids = self.folder / "ids.txt"
            write_lines(pred,[self.row])
            ids.write_text(self.row["id"],encoding="utf8")
            run = self.folder / str(i)
            prepare(argparse.Namespace(split="dev", predictions=pred, run=run, ids_file=ids,
                                       sumo=None, timeout=10, range_tolerance=0.1))
            items = lines(run / "items.jsonl")
            with patch("judge.settings",return_value=("local","unused")), patch("judge.infer",return_value={
                    "score":other_score,"reason":"other requirements only"}):
                args = argparse.Namespace(model="mock",limit=None,timeout=1,retries=0)
                self.assertEqual(grade(args,run,items),0)
            result = report(run,items)
            self.assertEqual(result["overall"]["mean_score"],final)
            self.assertEqual(result["numeric_range"]["failed"],int(miles==0.112))


if __name__ == "__main__":
    unittest.main()
