# TrafficForge dataset

交通仿真路网多任务数据集。当前版本是可运行、可复核的试制集，支持合成路网、OpenStreetMap 真实路网和已有 SUMO 路网编辑。所有样本均有 SUMO 网络工件与机器可读的验收信息。它是研究数据的起点，尚未经专家双人标注，也不是之前规划的完整 8,000 / 600 / 900 数据集。

## 目录

- `data/train.jsonl`, `data/dev.jsonl`, `data/test.jsonl`：每行一题。
- `data/artifacts/`：参考 SUMO 网络、编辑输入以及真实地图 OSM 原始快照和地点检索响应。
- `scripts/build_pilot.py`：构造试制数据；联网获取地图，依赖 Python 标准库和 SUMO 的 netconvert。
- `scripts/validate.py`：离线验证清单、划分、标签和 SUMO 网络加载。
- `DATASET.md`：字段、来源、划分和评测口径。

运行：

```bash
python scripts/build_pilot.py --netconvert /path/to/netconvert --sumo /path/to/sumo
python scripts/validate.py --sumo /path/to/sumo
```

构建真实数据时会查询公共 Nominatim 和 Overpass 服务，具体结果随地图更新时间变化。提交的 `data/artifacts/` 是冻结快照；重新下载将改变数据，不应与已有测试结果混用。构建脚本拒绝覆盖已存在的数据目录。

## 授权与来源

真实地图来自 © OpenStreetMap contributors，按 [ODbL](https://www.openstreetmap.org/copyright) 授权。OSM 数据记录了获取时间与查询结果。请在分发衍生数据前根据实际用途核查授权条款。
