# 当前有效数据集

只保留一个运行版本：

`expansion/releases/2026-09-10-random-dev-test-v1/`

- 100 家公司，107 条轨迹，2,152 条观察记录。
- 按公司随机划分 dev/test = 60/40，分别 64/43 条轨迹。
- `cases.jsonl`、`observations.jsonl`：样本及观察数据。
- `outcomes.jsonl`：单独保存结果标签，不作为运行输入。
- `partitions.jsonl`：固定划分。
- `obligors.csv`：最终公司清单。
- `manifest.json` 及审计文件：来源、质量与标签核验依据。

最终版本完整保留，不修改新闻、标签或划分。未核验的结果仍是未知，不能当作负例。

## 参考材料

保留 `candidates.csv`、`candidates_review.csv` 和 `contemporary/candidates.csv`，用于历史候选与复核追溯；它们不是另外三份可运行数据集。

`builders/` 保留构建代码，`expansion/` 中少量来源清单保留追溯信息。运行当前工作流无需重新抓取或构建。

旧版本、原始抓取和中间产物已移出项目，位置与恢复方式见 [CLEANUP.md](CLEANUP.md)。旧版脚本需要其历史输入，不能直接按当前目录重新运行。
