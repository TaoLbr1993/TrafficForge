# TrafficForge 路网评测工具

当前 300 道候选题使用 `v2.py`，数据包、输入格式和评分命令见 [v2 数据集说明](../dataset_design_v2/README.md)。运行 `python evaluation/check_v2_references.py` 可离线验证 300 组参考工件及限速、流量两项错误对照。

用于本仓库的 `data/{train,dev,test}.jsonl`。每个被测系统提交一份预测清单，本工具读取最终 SUMO 地图，调用一个云端视觉语言模型统一评分，并可导出双人人工盲评材料。试制集 0.1.1 的 dev/test 各有 10 题；同一底图的不同题共享 group_id。

## 准备环境

在 **TrafficForge 仓库根目录**运行以下 PowerShell 示例。需要 Python 3.10+、Pillow 和 SUMO。使用与数据集相同的 SUMO 1.28.0 可减少导入差异。工具优先查找 PATH 中的 sumo，其次尝试本仓库的 `.deps/sumo/bin/sumo.exe`；可通过 `--sumo` 指定其他位置。

```powershell
python -m pip install -r evaluation/requirements.txt
```

## 接入一个被测系统

每行一个 JSON 对象，编码 UTF-8。ID 必须与所选 split 相同。`net` 相对于**预测清单所在目录**，也可使用绝对路径。不同系统放在不同清单和 run 目录。示例：

```jsonl
{"id":"dev-synthetic-00","net":"maps/intersection.net.xml"}
{"id":"dev-synthetic-00-edit","status":"unsupported"}
{"id":"dev-place-04","status":"timeout"}
```

合法状态为 `ok`（默认，必填 net）、`failed`、`timeout`、`infrastructure_error` 和 `unsupported`。`timeout` 及有证据确认的外部服务故障 `infrastructure_error` 不评分、不计入成绩分母，并分别报告；例如 `{"id":"dev-place-04","status":"infrastructure_error","reason":"overpass_download_failed"}`。不能只因为地图生成失败就推定为服务故障。缺失 ID 自动计 0；重复 ID、split 外 ID 或清单格式错误会中止。参考答案只供评测端使用。用真实模型生成地图时仅交付题目与任务输入，不向生成器公开 reference_net、expected 或编辑补丁。

```powershell
python evaluation/evaluate.py prepare --split dev --predictions submissions/baseline/dev.jsonl --run evaluation/runs/baseline-dev
```

如果评测 1–3 公里版本，在 `prepare`、`range-report` 或 `reference-predictions` 命令中加入 `--data-dir data_1_3km`，并使用该版本的样本 ID 和对应结果；不应把旧版本预测与新参考地图混用。

prepare 会对每份预测实际运行 SUMO 加载检查，生成各题的 `packet.json` 和 PNG。数据、原图和预测均只读。不存在、无效或不能加载的输出计 0，SUMO 加载超时单独记录且不计分。工具环境错误应修复后再运行。prepare 使用新 run 目录，避免将不同预测混在一起。

## 云端模型评分

**距离容差**：若真实地图题的标注包含 `expected.radius_m = x`，使用已执行取图的距离（统一为米）作范围子项。在闭区间 `[(1−a)x, (1+a)x]` 内通过，默认 `a=0.10`，可在 `prepare` 或 `range-report` 加 `--range-tolerance 0.20` 改为 20%。矩形经纬度边界题不隐式换算成半径。

单独核验范围不需要图片、云裁判或重跑 ChatSUMO-Agent：

```powershell
python evaluation/evaluate.py range-report --split train --predictions experiments/chatsumo_agent/runs/train_real_retry_v1/predictions.jsonl --range-tolerance 0.10
```

该命令只统计所提交清单中的半径题；可用 `--out` 保存新 JSON。读取地图旁的 `trace.json`，也可在预测行用 `trace` 指定相对清单的路径。要求题号一致、整次生成成功、BUILD_NET 成功且 observation.net 指向提交的网络，再用实际调用的 distance_miles × 1609.344。ChatSUMO-Agent 用该距离计算下载矩形的四向半跨度，返回完整 OSM 道路，因此该值只核验请求的空间尺度。距离符合不会自动证明地点正确、边界为圆或图中所有道路端点都在圈内。

其他系统可在预测清单中用 `range_metadata` 指向由执行适配器保存的可信回执 JSON，字段为 `id`、`status:"ok"`、`net_sha256`、`executed_radius_m`、`query_shape`（circle/bbox/unknown）。不要用模型回复、参考标签或输入文本补写回执。

在新建的整题评测中，通过数值检查后裁判只评其他要求；数值越界时整题最高 1 分。证据缺失、取图失败或 trace 与工件不符，数值子项为 `unverified`，单列数量，不计入已核验范围通过率。对于有可加载输出但缺少范围回执的其他系统，整题仍按既有路网证据评估，图像判断不会填作数值通过。旧 run 的裁判分数不变；采用新口径的整题结果须重新 prepare 到新目录并评审。固定同一个 a 后再比较模型。

使用支持 **Chat Completions、文本和 Base64 PNG 输入**的多模态模型。下面以 OpenAI 的接口为例。先在本机配置 `OPENAI_API_KEY`；也可自行设置默认的 `TRAFFICFORGE_JUDGE_API_KEY` 并省略 `--key-env`。模型参数请换成账号可用的视觉模型，研究评测宜使用固定版本。

```powershell
python evaluation/evaluate.py judge --run evaluation/runs/baseline-dev --base-url https://api.openai.com/v1 --model gpt-4o-2024-08-06 --key-env OPENAI_API_KEY
python evaluation/evaluate.py report --run evaluation/runs/baseline-dev
```

其他兼容服务可传入自己的 `--base-url`（截至 API 前缀，例如 `https://供应商域名/v1`）、模型 ID 和密钥变量名。还可用 `TRAFFICFORGE_JUDGE_BASE_URL`、`TRAFFICFORGE_JUDGE_MODEL` 配置。密钥不写入清单或评分结果。远程请求使用 HTTPS；一次请求包含题面、评测参考条件、地图事实、候选图及适用的参考/编辑前地图。仅在运行 judge 时调用云服务，费用按所选服务计。

裁判对每一题给出整数分数与理由：2 完成全部实质要求；1 主要目标可辨但有实质遗漏或错误；0 核心任务失败。统一裁判提示词见 `judge.py`。判分材料：

| 任务 | 裁判获得的证据 |
|---|---|
| synthetic | 题面、标注条件、输出图，以及有向道路起终点、路口控制、路口间距离和车道限速。允许不同 ID 和等价路口布局 |
| real_map | 冻结参考路网和预测图、范围/投影、5 米栅格道路覆盖、抽样有向可达性。在同一米制投影中扣除 SUMO 偏移，以参考范围绘制两图 |
| edit | 编辑前与输出图，目标道路前后限速、其他限速改动、未匹配的道路和车道连接变化 |

真实地图若采用不同的标准投影，可选安装 `pyproj` 以对齐。缺少可用投影时，会标记无法验证地理对齐，分别绘图，不计算共同栅格和路由数据。覆盖和可达性是裁判参考证据：探针最多选 12 条有向边，按有向起终点 10 米容差匹配，在外部边的车道连接汇总图中检查可达性；该近似依赖道路分段，不等于正式 APLS 或任意车型的完整路由验证。编辑差异适用于当前试制集，按道路端点坐标及 ID 对齐；不唯一的平行边列为未匹配。真实地图的参考是冻结的 OSM 转换结果。

**输出**：`results.jsonl` 保存逐题状态、分数及理由；超时和基础设施故障题的 `score` 为 null。`summary.json` 同时列出总题数、超时数、基础设施故障数、可评分题数、已评分题数及待评数。完全完成率和平均分以**可评分题数**为分母；存在可评分待评题、或该组没有可评分题时，结果为 null。三类任务完全完成率的等权宏平均只在各类都有有效结果时计算。这些分数是条件结果，应与排除的题数和题目总数一起报告。历史 run 中按旧规则保存的超时会在重新执行 `report` 时识别并排除，无需重跑模型；既有 `failed` 不能在缺少证据时自动改成基础设施故障。run 中也保留 SUMO 版本、清单哈希、实际裁判配置、每次成功回复的模型标识和服务返回的用量。裁判 HTTP 错误或回复格式不合规记作待评，不转成 0 分；含待评样本的汇总分数为 null。重跑同一 judge 命令仅补评未成功题目；同一 run 请串行执行。使用新的裁判配置时创建新 run。

若先验证供应商和费用，可在 judge 命令末尾加 `--limit 1`，移除限制后从下一题继续。比较多个系统时，保留完整 split 的主结果，并可以在同一 split 上执行：

```powershell
python evaluation/evaluate.py compare --run evaluation/runs/baseline-dev --run evaluation/runs/ours-dev --out evaluation/runs/common-dev.json
```

共同子集排除至少一个系统明确提交 `unsupported`、超时或外部基础设施故障的题。缺交及加载失败仍计 0；比较前请按预先确定的支持范围提交状态。应同时报告共同子集的题数以及被排除的情况。

## 真人盲评与一致性

```powershell
python evaluation/human.py export --run evaluation/runs/baseline-dev --out evaluation/runs/audit-dev --per-task 3 --seed 42
```

可重复传 `--run` 将不同系统在同一 split 上的输出混合打乱。超时和基础设施故障题不进入盲评；每种任务、每个系统最多抽 `--per-task` 份，按输出抽样，不能把同源地图的多份输出当作独立地点。只发 `audit-dev/review/`：打开其中的 `index.html` 查看材料，两位评审独立填写 `rater-a.csv` 和 `rater-b.csv` 的 score 列（0/1/2）。不要给评审分发外层 `private_key.json`（盲号的回查表）或 run 的裁判结果。

```powershell
python evaluation/human.py compare --pack evaluation/runs/audit-dev --a evaluation/runs/audit-dev/review/rater-a.csv --b evaluation/runs/audit-dev/review/rater-b.csv
```

完成云端及两份人工评分后，`agreement.json` 给出总体与分任务的人–人、人–裁判完全一致率、线性加权 Cohen's κ，以及裁判给 2 分而两位评审都给低分的个数和裁判给满分的分母。样本少或分数为同一常量时，κ 可能无法定义，记为 null。

## 不用密钥的自检

```powershell
python evaluation/evaluate.py reference-predictions --split dev --out evaluation/runs/smoke/reference.jsonl
python evaluation/evaluate.py prepare --split dev --predictions evaluation/runs/smoke/reference.jsonl --run evaluation/runs/smoke/run
python evaluation/test_evaluation.py
```

第一条命令用参考地图构建本地冒烟测试输入，不能作为被测模型成绩。集成测试使用真实 SUMO 和本地模拟 HTTP 服务检查发图、解析、断点补评、人工抽样和一致性统计，不访问付费服务。
