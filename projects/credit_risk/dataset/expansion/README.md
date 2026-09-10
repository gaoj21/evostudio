# Credit Risk 数据集扩充版

当前使用 `releases/2026-09-09-outcomes-v2/`。本轮核验 64 个结局、修正 40 个日期，剩余 24 个负样本随访证据不足。当前为 100 家、107 个窗口、4,427 份独立材料、2,152 次观察；13 项测试通过。详细结果、逐家公司来源及重建命令见 [结局核验报告](../review/OUTCOME_REVIEW_2026-09-09.md)。下文保留上一版扩容记录，数值以本段及当前 manifest 为准。

此前发布目录：`releases/2026-09-09-web-v3/`。这是可运行的监控候选集，保留尚未核验的标签状态，**不是已完成金标的评测集**。此前 r10 和 web-v1/v2 是历史快照；使用 web-v3。

## Web Search 补充（2026-09-09）

对现有 100 家公司全部执行 Web Search，首轮 100 条查询，简称补搜 20 条，最后补搜 2 条，共 122 条查询。479 个去重候选来源保存在 `web_sources.json`，100 家均有 SEC 路径中的 CIK 匹配。该文件随发布快照保存并纳入校验值；链接仍需内容及公开日期核验，不能把 479 当成新增全文或金标。搜索仅替代本轮慢速新闻 API 的发现环节，未修改线上 API 实现，也未声称完成历史新闻覆盖。

本轮实际新增 3 条核验摘要、2 个退出破产监控窗口：Vroom 和 Vertex。修正两家原候选结局日期，把进入破产与退出破产分开。扩容脚本现在保留 R08 已修正窗口，避免重新采用旧候选日期；因此独立材料数从 4,683 降至 4,383，属于边界校正，不是抓取失败。

## 当前发布统计

| 指标 | 数量 |
|---|---:|
| 企业 | 100 |
| 企业观察窗口 | 107 |
| 从原始 41 家保留的窗口 | 39 |
| 从已下载材料恢复的公司窗口 | 59 |
| 新增一手来源监控事件窗口 | 9 |
| 去重后的独立材料 | 4,383 |
| 其中一手资料摘要 | 11 |
| 按材料公开日展开的观察记录 | 2,162 |
| 新事件小集合的观察记录 | 48 |

原来的 Auto Parts 4Less 和 Silver Star 在严格实体匹配及校正窗口内没有可用材料，留在排除清单；没有拿噪声填充数量。其余未恢复公司也有排除原因。59 个窗口只有公告，这种稀疏场景不再因没有新闻而被丢弃。

同一企业可有不同观察窗口，不能把 107 当作 107 家企业，或把 2,162 次观察当作独立样本。跨窗口复用的文件按 document_id 去重，manifest 同时报告引用总数和独立材料数。

## 新增的风险路径

| 案例 | 公开材料 | 用途 |
|---|---|---|
| AMC 2024、2025 再融资 | 发行人公告及 SEC 附件 | 债务到期延长、融资支持，不自动判低风险 |
| iHeartMedia 2024 债务交换 | 发起与完成两次公告 | 区分提议与完成，观察记忆能否更新 |
| JetBlue 2025-01 财务披露 | 发行人业绩公告 | 融资支持与亏损并存的混合信号 |
| Carvana 2025-03 评级上调 | S&P 原始评级行动 | 区分信用评级与股票分析师评级 |
| Boeing 2024-11 负面观察 | S&P 原始评级行动 | 维持评级、负面观察和股权融资同时存在 |
| FAT Brands 2025-11 债务加速到期 | 本地缓存的一手 SEC 8-K | 保留子公司证券化发行主体，区分加速到期与破产 |

来源链接、公开日期和简短事实摘要在 `curated_events.json`。累计 11 条材料是**有来源的助手转述**，representation 明确标记为 `assistant_paraphrase_of_primary_source`，不是冒充抓取的新闻全文。其余材料是已有缓存正文/标题。

## 文件

- `cases.jsonl`：企业、窗口和完整可用证据，不含结局标签。
- `observations.jsonl`：按公开日期展开的工作流输入，包含 company、cik、as_of、news_batch、filing_batch。每条只输入该日的新材料，不反复塞入过去全部新闻。
- `curated_observations.jsonl`：9 个新事件的 48 条观察，适合先做小范围调试。
- `outcomes.jsonl`：独立的标签、核验状态及来源。不应输入模型。
- `obligors.csv`：100 家企业的身份清单，不含风险标签，供 grounding 使用。
- `partitions.jsonl`：企业分组，旧样本和其衍生窗口保持同组；不是统一时间切分的金标测试集。
- `inventory.jsonl`：105 家原候选公司的纳入/排除结果。
- `excluded_news.jsonl`：重复标题、实体待复核、明确体育同名候选的记录，分别标记，不能全部当作真噪声。
- `backfill_queue.jsonl`：覆盖范围、材料稀疏度、标签待办和后续查询建议。
- `manifest.json`：统计、输入/输出/代码校验值。

## 实际可用于什么

可以用于工作流连通性、公告主导场景、实体匹配以及记忆时序调试。审核状态覆盖全部窗口，但其中只有 19 个窗口带本轮/上一轮的一手事件核验，88 个窗口的结局仍待核验。逐时点 risk_level 均未编造，因此 gold_samples=0，不能直接报告告警准确率。

新增了风险路径，但不意味着已实现行业均衡：92 个窗口的行业仍是 unknown，只有新事件涉及公司的粗行业分类；下一轮应完成行业标注后按缺口扩容，避免生物医药等行业过重。尚未证明正常观察窗口内“未发生任何信用事件”，负样本不得自动当低风险。

## 接到 Studio

Studio 默认仍使用原数据，现有历史记忆未改动。Grounding 支持环境变量 `EAX_STUDIO_OBLIGORS_FILE`，可指向发布目录的 `obligors.csv`；原默认清单不变。例如从仓库根目录启动一个独立的数据环境：

```sh
EAX_STUDIO_DATA_DIR=/tmp/eax-credit-risk-expansion \
EAX_STUDIO_OBLIGORS_FILE=projects/credit_risk/dataset/expansion/releases/2026-09-09-web-v3/obligors.csv \
uv run evoagentx-studio
```

独立环境需要导入 Credit Risk Monitoring 工作流。在批量运行中上传按 case_id 选出的观察记录，先不启用评分；不要把 outcomes 或 annotated_samples 上传成模型输入。sample_id 是不含正负类别及事件日期的 opaque case_id，供现有批量调度器把同一窗口按时间串行执行。

**记忆隔离：**同一企业不同窗口会读写相同企业记忆。比较独立案例/提示词版本时，每个 case 使用独立工作流或在独立实验环境中重置记忆；不要把全部窗口在共享旧记忆上跑完，再当成独立评测结果。本文命令不会自动发起任何模型调用。

## 时间与证据边界

- 输入只在 available_at 当天出现；每条 observation 的 window_end 等于 as_of，不暴露最终结局日。
- 法律事件日与公开材料日期独立保存；无法从当前材料确认的 event_date 保持 null。
- 新事件是监控/事件识别案例，包含公开事件当日证据，**不用于宣称提前预警**。
- 原缓存日级时间与新闻正文的历史版本未逐条验证，不能宣称严格的历史时点金标。
- SEC 输入提取实质 Item 标题后的段落，附原文偏移；这比只取封面前 1500 字更有用，但仍是有预算的节选，完整文本在 cases 中。
- 未在本轮补抓所有缺失日期；证据稀疏和窗口完整是两回事。

## 重建与测试

从仓库根目录执行；目录必须为空，拒绝覆盖发布版本或实时数据目录：

```sh
python3 projects/credit_risk/dataset/builders/build_r10_expand.py \
  --discovery projects/credit_risk/dataset/expansion/web_sources.json \
  --output projects/credit_risk/dataset/expansion/releases/NEW_VERSION
.venv/bin/python -m pytest tests/dataset tests/studio/test_obligor_dataset.py -q
```

本轮验证：12 项测试通过；另核验全部源文件及产物校验值、观察记录日期、企业分组、标签分离、100 家身份清单。没有启动真实模型批量运行。
