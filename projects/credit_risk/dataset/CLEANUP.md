# 数据集清理

当前唯一运行版本：`expansion/releases/2026-09-14-eligible-trajectories-v1`。

首次目录清理时，原 107 条版本的文件 SHA-256 保持一致。随后按用户要求进行轨迹资格筛选，生成当前 53 条版本；保留轨迹的内容不变。

旧版本与原始抓取移至项目外归档：

`/Users/doby/Desktop/EvoAgentX-dataset-archive-20260914-152648`

归档内 `ARCHIVE_MANIFEST.json` 记录原相对路径和文件校验和，可按路径恢复。候选 CSV、构建代码作为参考保留；旧构建脚本如需重跑，先恢复对应历史输入。

## 轨迹资格筛选

当前有效版本为 `expansion/releases/2026-09-14-eligible-trajectories-v1`：53 家公司、53 条轨迹、648 条观察记录。原 107 条完整版本已归档至 `/Users/doby/Desktop/EvoAgentX-dataset-archive-20260914-152648/final-before-eligibility-filter/2026-09-10-random-dev-test-v1`。筛选只删除未满足当前规则的轨迹，保留轨迹内容和原 dev/test 归属；现为 dev 30 条 / test 23 条，不再宣称严格 6:4。历史运行结果没有改写。
