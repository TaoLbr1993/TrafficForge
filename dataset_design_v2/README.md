# TrafficForge v2 候选测试集

数据集有 300 道中文题。人工地图生成、真实地图查询和已有地图修改各 100 道，每类有 60 道指定交通流量。流量按行驶方向、每小时车辆数和时间区间核验。真实地图题使用六座城市的 OSM 冻结快照，涵盖 100 个不同节点，圆形、矩形各 50 题。修改题使用合成底图。数据仍待独立人工审核。

## 文件

| 内容 | 路径 |
|---|---|
| 模型可见的三份题单 | `pilot_views/*_public.jsonl` |
| 100 份输入地图和 20 份既有流量文件 | `assets/edit_inputs.zip` |
| 六份 OSM 原始快照、来源信息及下载元数据 | `assets/osm_sources.zip` |
| 评测端使用的人工地图、真实地图和修改标注 | `work_synthetic/full_samples.jsonl`、`work_real_traffic/samples.jsonl`、`work_edit_pilot/full_samples.jsonl` |
| 评测端使用的参考地图、交通需求和 SUMO 配置 | `assets/real_references.zip`、`assets/synthetic_edit_references.zip` |
| 数据包字节数、文件数和 SHA-256 | `assets/manifest.json` |

从仓库根目录运行以下命令。建议在新检出的仓库中解包，各文件会落到样本记录指定的相对路径。运行模型时只提供公开题面、题目指定的编辑输入和允许查询的 OSM 数据。参考标注与参考工件留在评测端。

```sh
python -m zipfile -e dataset_design_v2/assets/edit_inputs.zip .
python -m zipfile -e dataset_design_v2/assets/osm_sources.zip .
python -m zipfile -e dataset_design_v2/assets/real_references.zip .
python -m zipfile -e dataset_design_v2/assets/synthetic_edit_references.zip .
```

## 评分

v2 评价器读取执行后的 SUMO 工件。每题目录为 `<run>/cases/<id>/`，包含 `execution.json`。地图交付成功时，记录使用 `status="map_delivered"` 和 `net`。指定了流量的题目同时提供 `routes` 和经 SUMO 加载验证的 `flow_execution="loaded"`。文件路径可取绝对路径或相对于该执行记录的路径。人工地图还需在题目目录保存 `model.nod.xml`、`model.edg.xml`。真实地图的执行记录需要 `details.center`（纬度、经度）、`details.shape`（circle 或 rectangle）和 `details.size_m`（圆形半径或矩形边长，米）。

```sh
python evaluation/v2.py --run submissions/my-model --out evaluation/runs/my-model
```

`results.jsonl` 记录每题的地图、交通流判定与原因。`summary.json` 按任务汇总，并给出可判定题数和通过率。超时、外部服务故障、截断、缺少结果以及自动规则无法判定的情况单列。综合判定为通过需要满足地图与题目指定的流量要求。几何上可能等价、但尚未由规则确认的实现仍需复核。交通流检查使用需求文件与执行器的 SUMO 加载记录。历史试制集的评测命令见 [evaluation/README.md](../evaluation/README.md)。

六份快照来自 © OpenStreetMap contributors，适用 [Open Database License](https://www.openstreetmap.org/copyright)。同一来源的地图可能重叠，训练或选取验证集时应按地图来源和底图分组。
