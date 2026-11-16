"""Export a blinded sample and compare two independent CSV reviews to the cloud judge."""
import argparse
import csv
import html
import json
import random
import shutil
import sys
from collections import Counter
from pathlib import Path

from evaluate import ROOT, TASKS, is_unscored, lines, rating, records
from judge import json_write


def blank_csv(path, ids):
    with path.open("w", newline="", encoding="utf-8-sig") as stream:
        out = csv.writer(stream)
        out.writerow(["blind_id","score","comment"])
        out.writerows([key,"",""] for key in ids)


def export(args):
    runs = [p.resolve() for p in args.run]
    if len(set(runs)) != len(runs) or args.per_task < 1 or args.out.exists():
        raise ValueError("use distinct runs, positive --per-task and a new output folder")
    rng = random.Random(args.seed)
    indexed = []
    for run in runs:
        metadata = json.loads((run / "run.json").read_text("utf8"))
        indexed.append((metadata, lines(run / "items.jsonl")))
    split = indexed[0][0]["split"]
    if any(m["split_sha256"] != indexed[0][0]["split_sha256"] for m, _ in indexed):
        raise ValueError("all runs must use the same split and dataset revision")
    pool = []
    for task in TASKS:
        for run, (_, items) in zip(runs, indexed):
            candidates = [row for row in items if row["task"] == task and not is_unscored(row)]
            pool.extend((run,row) for row in rng.sample(candidates,min(args.per_task,len(candidates))))
    rng.shuffle(pool)
    dataset = records(ROOT / "data" / (split + ".jsonl"))
    pack = args.out / "review"
    pack.mkdir(parents=True)
    keys, entries = {}, []
    for i, (run, item) in enumerate(pool, 1):
        key = f"M{i:04d}"
        keys[key] = {"run":str(run),"id":item["id"],"task":item["task"]}
        folder = run / "cases" / item["id"]
        card = "<article><h2>" + key + "</h2><p>" + html.escape(dataset[item["id"]]["instruction"]) + "</p>"
        if item["state"] == "ready":
            packet = json.loads((folder / "packet.json").read_text("utf8"))
            check = packet["evidence"].get("range_check", {})
            if check.get("status") in ("pass", "fail"):
                verdict = "通过" if check["status"] == "pass" else "不通过，整题最高 1 分"
                card += (f"<p>范围数值已核验：{check['measured_m']:g} 米，"
                         f"允许 {check['inclusive_interval_m']} 米，{verdict}。"
                         "请单独判断地点、路网和题面指定的形状。</p>")
            for file in packet["images"]:
                output = key + "-" + file
                shutil.copyfile(folder / file, pack / output)
                label = {"reference.png":"编辑前底图" if item["task"]=="edit" else "参考路网",
                         "candidate.png":"待评路网"}[file]
                card += f'<figure><figcaption>{label}</figcaption><img src="{output}"></figure>'
            info = {k:packet[k] for k in ("expected","evidence")}
            card += "<details><summary>查看路网事实和参考条件</summary><pre>" + html.escape(json.dumps(info,ensure_ascii=False,indent=2)) + "</pre></details>"
        else:
            card += "<p>无可加载的路网。状态 " + html.escape(item["reason"]) + "</p>"
        entries.append(card + "</article>")
    page = """<!doctype html><html lang="zh"><meta charset="utf-8"><title>路网人工盲评</title>
<style>body{font:16px system-ui;max-width:1000px;margin:auto}article{border-top:1px solid #aaa;padding:1em}
figure{display:inline-block;margin:5px}img{width:360px;max-width:100%}pre{white-space:pre-wrap}</style>
<h1>路网独立评审</h1><p>逐题查看需求、图与路网事实，在自己的 CSV 中按编号填写：
2 完成全部实质要求；1 主要目标可辨但有遗漏或错误；0 核心目标错误或没有可加载的路网。
忽略渲染风格。合成路口可有多种合法布局。真实路网与参考范围比较；编辑任务同时核对目标变化与其他道路。
分歧或判断依据可记入 comment。</p>"""
    (pack / "index.html").write_text(page + "".join(entries) + "</html>",encoding="utf8")
    blank_csv(pack / "rater-a.csv", keys)
    blank_csv(pack / "rater-b.csv", keys)
    json_write(args.out / "private_key.json", {"seed":args.seed,"samples":keys})
    print(f"Exported {len(keys)} outputs. Share only {pack} with raters.")


def read_ratings(path, keys):
    values = {}
    with path.open(encoding="utf-8-sig",newline="") as stream:
        for row in csv.DictReader(stream):
            key = row["blind_id"]
            if key in values or row["score"].strip() not in ("0","1","2"):
                raise ValueError(f"missing/invalid rating or duplicate ID in {path}: {key}")
            values[key] = int(row["score"])
    if set(values) != set(keys):
        raise ValueError(f"IDs differ in {path}")
    return values


def agreement(a,b):
    n = len(a)
    ca, cb = Counter(a),Counter(b)
    observed = sum(abs(x-y) for x,y in zip(a,b))
    chance = sum(abs(i-j)*v*w for i,v in ca.items() for j,w in cb.items())
    return {"n":n,"exact_agreement":sum(x==y for x,y in zip(a,b))/n if n else None,
            "linear_weighted_kappa":1-observed*n/chance if chance else None}


def compare(args):
    keys = json.loads((args.pack / "private_key.json").read_text("utf8"))["samples"]
    aa, bb = read_ratings(args.a, keys), read_ratings(args.b, keys)
    jj, indexed = {}, {}
    for key, info in keys.items():
        run = Path(info["run"])
        if run not in indexed:
            indexed[run] = records(run / "items.jsonl")
        value = rating(run, indexed[run][info["id"]])
        if value is None:
            raise ValueError("complete the cloud ratings before comparing: " + str(run))
        jj[key] = value["score"]
    def summarize(ids):
        a,b,j = ([ratings[i] for i in ids] for ratings in (aa, bb, jj))
        return {"human_a_vs_b":agreement(a,b), "judge_vs_a":agreement(j,a),"judge_vs_b":agreement(j,b),
                "judge_full_both_humans_lower":sum(z==2 and x<2 and y<2 for x,y,z in zip(a,b,j)),
                "judge_full_count":j.count(2)}
    result = {"overall":summarize(keys),"by_task":{task:summarize([k for k in keys if keys[k]["task"]==task]) for task in TASKS}}
    json_write(args.pack / "agreement.json", result)
    print(json.dumps(result, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd",required=True)
    p = sub.add_parser("export")
    p.add_argument("--run",action="append",type=Path,required=True)
    p.add_argument("--out",type=Path,required=True)
    p.add_argument("--per-task",type=int,default=10,help="per task per system; capped by available outputs")
    p.add_argument("--seed",type=int,default=42)
    p = sub.add_parser("compare")
    p.add_argument("--pack",type=Path,required=True)
    p.add_argument("--a",type=Path,required=True)
    p.add_argument("--b",type=Path,required=True)
    args=parser.parse_args()
    (export if args.cmd=="export" else compare)(args)


if __name__ == "__main__":
    try:
        main()
    except (ValueError,OSError,KeyError) as exc:
        sys.exit(str(exc))
