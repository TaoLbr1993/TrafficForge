"""Integration tests use an actual pilot split and SUMO, but never contact a cloud provider."""
import argparse
import base64
import csv
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from evaluate import ROOT, common_subset, lines, prepare, prepare_case, records, report, write_lines
from human import agreement, compare, export
from judge import grade


class EvaluationTest(unittest.TestCase):
    def test_linear_kappa(self):
        self.assertEqual(agreement([0,1,2], [0,1,2])["linear_weighted_kappa"], 1)
        self.assertIsNone(agreement([2,2], [2,2])["linear_weighted_kappa"])

    def test_pilot_cloud_and_blind_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            where = Path(tmp)
            run = where / "run"
            predictions = where / "pred.jsonl"
            samples = records(ROOT / "data/dev.jsonl")
            selected = {}
            for row in samples.values():
                selected.setdefault(row["task"], row)
            rows = [{"id":v["id"],"net":str(ROOT / v["artifacts"]["reference_net"])} for v in selected.values()]
            # Feed the unchanged input for the edit: loadable, but the task was not completed.
            rows[1:] = [dict(r, net=str(ROOT / samples[r["id"]]["artifacts"]["input_net"]))
                        if samples[r["id"]]["task"]=="edit" else r for r in rows[1:]]
            missing = sorted(set(samples)-{r["id"] for r in rows})
            bad = where / "bad.xml"
            bad.write_text("<net",encoding="utf8")
            rows.append({"id":missing[0],"net":bad.name})
            rows.append({"id":missing[1],"status":"unsupported"})
            rows.append({"id":missing[2],"status":"timeout"})
            write_lines(predictions,rows)
            prepare(argparse.Namespace(split="dev", predictions=predictions, run=run, sumo=None, timeout=10))
            items = lines(run / "items.jsonl")
            by_id = {r["id"]:r for r in items}
            self.assertEqual(by_id[missing[0]]["reason"], "invalid_network")
            self.assertEqual(by_id[missing[1]]["reason"], "unsupported")
            self.assertEqual(by_id[missing[2]]["state"], "timeout")
            self.assertEqual(sum(r["state"]=="ready" for r in items), 3)
            self.assertIsNone(report(run,items)["overall"]["full_success_rate"])
            edit = json.loads((run/"cases"/selected["edit"]["id"]/"packet.json").read_text("utf8"))
            self.assertEqual(edit["evidence"]["changes_from_input"]["speed_changes_count"], 0)
            real = json.loads((run/"cases"/selected["real_map"]["id"]/"packet.json").read_text("utf8"))
            occ = real["evidence"]["occupied_5m_cells"]
            self.assertEqual(occ["reference"], occ["overlap"])
            calls, gate = [], {"bad":True}

            class MockCloud(BaseHTTPRequestHandler):
                def log_message(self,*args):
                    pass
                def do_POST(self):
                    payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    parts = payload["messages"][1]["content"]
                    info = json.loads(parts[0]["text"])
                    pngs = [base64.b64decode(p["image_url"]["url"].split(",")[1]) for p in parts if p["type"]=="image_url"]
                    calls.append((self.path,self.headers["Authorization"],payload["model"],info["task"],pngs))
                    # Malformed provider response for one sample on the first run.
                    if info["task"]=="edit" and gate["bad"]:
                        answer = {"score":9,"reason":"invalid test result"}
                    else:
                        answer = {"score":1 if info["task"]=="edit" else 2, "reason":"mock evidence check"}
                    body = json.dumps({"model":"mock","choices":[{"message":{"content":json.dumps(answer)}}]}).encode()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

            with ThreadingHTTPServer(("127.0.0.1",0),MockCloud) as server:
                thread = threading.Thread(target=server.serve_forever,daemon=True)
                thread.start()
                cfg = argparse.Namespace(model="mock", base_url=f"http://127.0.0.1:{server.server_port}/v1",
                                         key_env="EVALUATION_TEST_KEY",timeout=5,retries=0,limit=None)
                try:
                    with patch.dict(os.environ,{"EVALUATION_TEST_KEY":"local-dummy"}):
                        cfg.limit = 1
                        self.assertEqual(grade(cfg,run,items),0)
                        self.assertEqual(len(calls),1)
                        cfg.limit = None
                        self.assertEqual(grade(cfg,run,items),1)
                        self.assertEqual(report(run,items)["overall"]["pending"],1)
                        gate["bad"]=False
                        self.assertEqual(grade(cfg,run,items),0)
                        self.assertEqual(grade(cfg,run,items),0)
                finally:
                    server.shutdown()
                    thread.join()
            self.assertEqual(len(calls),4)
            for path, token, model, task, images in calls:
                self.assertEqual((path,token,model),("/v1/chat/completions","Bearer local-dummy","mock"))
                self.assertEqual(len(images),1 if task=="synthetic" else 2)
                self.assertTrue(all(img.startswith(b"\x89PNG") for img in images))
            summary = report(run,items)
            self.assertEqual(summary["overall"]["full_success_rate"],2/(len(samples)-1))
            self.assertEqual(summary["overall"]["timeout"],1)
            self.assertEqual(summary["overall"]["eligible"],len(samples)-1)
            self.assertIsNone(records(run / "results.jsonl")[missing[2]]["score"])
            second = where/"no-predictions"
            empty = where/"empty.jsonl"
            write_lines(empty, [])
            prepare(argparse.Namespace(split="dev", predictions=empty, run=second, sumo=None, timeout=10))
            common = common_subset([run, second])
            self.assertNotIn(missing[1], common["common_ids"])
            self.assertNotIn(missing[2], common["common_ids"])
            self.assertIn(missing[0], common["common_ids"])
            self.assertEqual(common["runs"][str(second.resolve())]["overall"]["full_success_rate"],0)
            pack = where/"human"
            export(argparse.Namespace(run=[run],out=pack,per_task=10,seed=7))
            folder = pack/"review"
            mapping = json.loads((pack/"private_key.json").read_text("utf8"))["samples"]
            self.assertEqual(len(mapping),len(samples)-1)
            for label in ("a","b"):
                with (folder/f"rater-{label}.csv").open("w",encoding="utf-8-sig",newline="") as f:
                    out=csv.writer(f)
                    out.writerow(["blind_id","score","comment"])
                    for key, info in mapping.items():
                        row=by_id[info["id"]]
                        out.writerow([key, (1 if info["task"]=="edit" else 2) if row["state"]=="ready" else 0,""])
            compare(argparse.Namespace(pack=pack,a=folder/"rater-a.csv",b=folder/"rater-b.csv"))
            result=json.loads((pack/"agreement.json").read_text("utf8"))
            self.assertEqual(result["overall"]["judge_vs_a"]["exact_agreement"],1)
            self.assertNotIn("private_key", (folder/"index.html").read_text("utf8"))

    def test_legacy_and_sumo_timeouts_are_unscored(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            sample = next(iter(records(ROOT / "data/dev.jsonl").values()))
            submission = {"id": sample["id"], "net": str(ROOT / sample["artifacts"]["reference_net"])}
            args = argparse.Namespace(predictions=run / "prediction.jsonl", timeout=1)
            import subprocess
            with patch("evaluate.subprocess.run", side_effect=subprocess.TimeoutExpired("sumo", 1)):
                timed_out = prepare_case(sample, submission, args, "sumo", run)
            self.assertEqual(timed_out, {"state":"timeout", "reason":"sumo_timeout"})
            rows = [
                {"id":"legacy", "task":"synthetic", "state":"failed", "score":0, "reason":"timeout"},
                {"id":"load", "task":"real_map", **timed_out},
                {"id":"failed", "task":"edit", "state":"failed", "reason":"failed"},
            ]
            result = report(run, rows)
            self.assertEqual(result["overall"]["timeout"], 2)
            self.assertEqual(result["overall"]["eligible"], 1)
            self.assertEqual(result["overall"]["score_0"], 1)
            self.assertIsNone(result["by_task"]["synthetic"]["full_success_rate"])
            self.assertIsNone(records(run / 'results.jsonl')['legacy']['score'])
            self.assertEqual(records(run / 'results.jsonl')['legacy']['state'], 'timeout')

    def test_infrastructure_failure_is_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            sample = next(iter(records(ROOT / "data/dev.jsonl").values()))
            submission = {"id": sample["id"], "status": "infrastructure_error", "reason": "overpass_download_failed"}
            result = prepare_case(sample, submission, argparse.Namespace(), "sumo", run)
            rows = [dict(id="service", task="real_map", **result),
                    {"id":"failed", "task":"edit", "state":"failed", "reason":"failed"}]
            summary = report(run, rows)
            self.assertEqual(summary["overall"]["infrastructure_error"], 1)
            self.assertEqual(summary["overall"]["eligible"], 1)
            self.assertEqual(summary["overall"]["score_0"], 1)
            self.assertIsNone(records(run / "results.jsonl")["service"]["score"])

    def test_subset_requires_all_selected_predictions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sample = next(iter(records(ROOT / "data/dev.jsonl").values()))
            ids = root / "ids.txt"
            ids.write_text(sample["id"] + "\n", encoding="utf8")
            preds = root / "predictions.jsonl"
            write_lines(preds, [])
            args = argparse.Namespace(split="dev", predictions=preds, run=root / "out", sumo=None,
                                      timeout=10, ids_file=ids)
            with self.assertRaisesRegex(ValueError, "must all have predictions"):
                prepare(args)
            write_lines(preds, [{"id": sample["id"], "status": "timeout"}])
            prepare(args)
            self.assertEqual(len(lines(args.run / "items.jsonl")), 1)


if __name__ == "__main__":
    unittest.main()
