# 数据集审核版

当前版本：`releases/2026-09-09-r09-v2/`。这是可追溯的审核中间版本，**不是新的金标准测试集**，尚未切换 Studio 的数据源。`2026-09-09-r09/` 是本轮早期快照，后续使用 v2。

## 本轮结果

- 对全部 41 个样本、4,534 条新闻生成审核记录，原始文件保持不变。
- 10 家正样本已有一手来源复核；其中 9 个法律事件日期与现有标签不同。来源、主体范围和事件类型见 `event_reviews.json`。
- 48 条明确的 Nikola 体育同名候选移入噪声侧；457 条缺乏明确实体证据的新闻进入待复核侧。其余 4,029 条仅是实体匹配候选，不代表主语或信用相关性已确认。
- 日期过滤与标题规范化去重后，共 3,735 条候选新闻输入。去重保留最早版本，不用后来的正文替换早期证据。
- 9 个窗口在校正后有未补抓区间；4 个样本的候选新闻不足 10 条。
- 尚余 16 个正样本的结局、15 个负样本的观察期限未完成核验；全部逐时点风险标签待标注，所以 gold_evaluation_samples 明确为 0。

## 文件用途

| 文件 | 用途 |
|---|---|
| annotated_samples.jsonl | 原始内容加逐条规则标记、样本审核状态；不直接喂模型 |
| outcomes.jsonl | 单独保存原标签、核验结局、日期、主体范围、来源和评估阻塞项 |
| candidate_clean.jsonl | 日期过滤、保守实体筛选及标题去重后的候选输入，非 gold |
| noise_challenge.jsonl | 同名噪声及实体待判定条目；两者通过 entity_status 区分，不能全部当噪声 |
| review_queue.jsonl | 41 个样本的待办及待补抓区间 |
| manifest.json | 源数据、审核规则、证据清单及产物的 SHA-256、统计和限制 |

`candidate_clean` 使用不含正负类别和事件日期的 opaque input_id；可在 outcomes 中找到其与原 sample_id 的映射。模型侧不含 label/type/split，也不含依赖未来条目计算的 cluster_size。标注版仍完整保存这些字段供审核。

新闻 date 目前只有日精度且未逐条核验真实首次公开时间。核验表 first_public_at 留空，不能将 petition date 直接当作新闻可用时间。一手材料仅用于审核结局，不会被自动注入事件之前的新闻窗口。

## 结局标签中的重要区别

- Auto Parts 4Less：assignment_for_benefit_of_creditors，不能作为已确认 Chapter 11。
- EchoStar：subsidiary_chapter_11，主体为 Hughes 子公司群，不能作为母公司破产。
- Sunnova：当前核验条目是母公司 2025-06-08 的事件；此前子公司事件需独立标注。
- Canoo：chapter_7，与重组型 chapter_11 区分。

## 重建

从仓库根目录运行：

```sh
python3 projects/credit_risk/dataset/builders/build_r09_review.py --output projects/credit_risk/dataset/review/releases/NEW_VERSION
```

输出目录必须为空；拒绝覆盖已有发布版本，也拒绝写入实时数据源目录。更新 `event_reviews.json` 后生成新目录，保留旧版本用于比较。单元测试：

```sh
.venv/bin/python -m pytest tests/dataset/test_review_release.py -q
```

## 下一步的标注顺序

1. 先处理事件主体错误、日期改变和窗口缺失，再完成其余结局及负样本观察期核验。
2. 人工复核实体归属，特别是 Nikola 的 138 条待确认记录；目前保守筛选会漏掉仅用简称的真实报道，不能把候选输入当成完整证据流。
3. 为真实报道标注主语/配角、信用相关性、独立事件、转载/律师招募稿、确认程度、首次公开日和解决证据。
4. 建立独立的行业、企业规模、风险结局、稀疏新闻与恢复路径配额；当前规则没有增加任何新企业，diversity 尚未解决。
5. 全部核验后发布 clean 与 robustness 两套评估集，冻结统一时间切分和企业分组；不把按破产倒计时推定的风险等级作为人工标签。
