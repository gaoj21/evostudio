# 当前有效数据集

唯一运行版本：`expansion/releases/2026-09-14-eligible-trajectories-v1/`。

| 指标 | 全部 | dev | test |
|---|---:|---:|---:|
| 公司 / 轨迹 | 53 / 53 | 30 / 30 | 23 / 23 |
| 观察记录 | 648 | 397 | 251 |

包含 1,006 份独立材料。由原 107 条轨迹筛选而来，移除 54 条不符合当前事件前正例评测规则的轨迹，同时过滤观察记录、公司清单及关联审计文件。保留样本的内容、ID 和原集合归属不变；筛选后不是严格 6:4。

保留条件：结局已经核验；目标为 Chapter 11、Chapter 7 或加拿大破产财产转让；事件主体为注册发行人；事件日期晚于观察窗口结束日。

**当前全部为正例，没有已核验负例。** 可用于评估目标事件发现率和预警提前量，不能报告整体二分类 accuracy 或误报率。符合该规则不等于材料已通过所有质量检查，仍存在稀疏轨迹及仅标题材料。

## 文件与使用

- `cases.jsonl`、`observations.jsonl`：轨迹及按公开日组织的观察数据。
- `outcomes.jsonl`：独立保存结局，不作为模型运行输入。
- `partitions.jsonl`、`dev/`、`test/`：固定集合归属与分集数据。
- `obligors.csv`：当前 53 家公司清单。
- `manifest.json` 及审计文件：数量、筛选规则、来源与文件校验和。

Slides 和当前统计使用 [当前数据说明](../docs/dataset/DATASET_CURRENT_PROFILE.md)。原始构建过程见 [历史构建记录](../docs/dataset/DATASET_CONSTRUCTION_AND_PROFILE.md)。

## 参考材料

`candidates.csv`、`candidates_review.csv` 和 `contemporary/candidates.csv` 是历史候选与复核记录，不是当前运行数据或公司清单。`builders/` 保留构建代码；运行当前工作流无需重新抓取。

旧版本、原始抓取和中间产物已移出项目，位置与恢复方式见 [CLEANUP.md](CLEANUP.md)。旧版脚本需要历史输入，不能直接按当前目录重新运行。
