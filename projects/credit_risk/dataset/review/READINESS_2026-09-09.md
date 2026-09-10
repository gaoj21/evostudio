# 2026-09-09 清洗与评测准备状态

当前 release：`../expansion/releases/2026-09-09-industry-v1/`，保留 100 家公司、107 个窗口、2152 个观察日。

## 已完成

- 238 条新闻的正文确认为首页行情、访问限制、网站关闭公告、导航、cookie 或验证码；正文降级为空，保留原始标题、来源、日期、文档 ID 和被隔离正文哈希。原文保留在不可变父版本 outcomes-v2。
- 清洗后 1912 条正文新闻引用、1903 条仅标题新闻引用、641 条申报文件引用、13 条人工一手来源摘要引用。仅标题不是完整正文证据。
- 相同文档 ID 或相同非空正文连接的公司合并分组，传递闭包防止共用文章跨 development / holdout。接触过 legacy_review 的整个分组留在 review。剩余 3 个跨公司相同正文簇是多公司真实报道，保留。
- 日期、观察日和证据引用的结构检查：0 个错误。这不证明网页正文未被历史修订。
- Studio 评分器移除“离破产多少天推算风险等级”和“负样本自动低风险”；风险等级仅接受同一天有证据引用的 reviewed 标注。Studio 与命令行均排除未核验负样本的误报统计。
- 本轮没有启动付费模型或批量评测。

## 尚未补齐，不能宣称正式评测就绪

- 24 个旧负样本：逐来源检查记录在 followup_checks_2026-09-09.json。部分来源只证明某个日期遵守债务契约，不能证明整个随访区间没有事件。全部仍为未核验。
- 随访定义：窗口结束后 90 天，目标为注册主体法律破产或已确认付款违约；常规再融资、评级变化、仅子公司破产不自动算目标事件。没有目标事件也不自动代表信用风险低。
- 2152 个观察日尚无经逐日证据审阅的风险等级。risk_review_queue.jsonl 是待办，不能用作答案。
- 行业字段已补齐：100 家匹配 SEC CIK，覆盖 58 个 SIC 行业代码、8 个粗粒度行业组。制造业 45 家，服务业 15 家，零售 15 家，运输/通信/公用事业 12 家，金融/保险/房地产 7 家，农业 3 家，采矿/油气 2 家，批发 1 家。仍不是行业平衡样本；这是当前 SEC 分类，只用于构成分析，不注入历史观察日输入。
- 新 release 的 outcomes 是隔离侧表；旧 Studio sample_json 评分入口不能自动读取它。正式批次需补充侧表评分接入。

所以暂不启动 100 家完整付费评测。以上问题需要补充证据或标注，不能通过改状态字段关闭。

## 可复现构建（项目根目录）

```sh
python projects/credit_risk/dataset/builders/build_r13_clean.py --source projects/credit_risk/dataset/expansion/releases/2026-09-09-outcomes-v2 --output projects/credit_risk/dataset/expansion/releases/NEW_CLEAN_VERSION --rules projects/credit_risk/dataset/review/body_quality_2026-09-09.json
python projects/credit_risk/dataset/builders/build_r12_readiness.py --release projects/credit_risk/dataset/expansion/releases/NEW_CLEAN_VERSION --output projects/credit_risk/dataset/review/releases/NEW_READINESS_VERSION
python -m pytest tests/dataset tests/studio/test_evaluation_report.py -q
```

输出目录必须为空。clean-v1 的源数据和输出摘要哈希记录在 manifest.json；不覆盖旧版本和默认 live 数据。

## 本次续补

- SEC 元数据缓存：`releases/2026-09-09-metadata-v1/`，100 家成功，0 个获取错误。可审计小型来源清单：`industry_annotations_2026-09-09.json`。
- 24 个随访资料包：`releases/2026-09-09-followup-v2/followup_packets.json`。768 份申报，其中 75 份 8-K、23 份 10-K/10-Q、11 份 6-K；其余主要为持股申报等。未找到 items 标记含 1.03 或 2.04 的申报。目录扫描不是正文逐份审阅，也不能证明没有违约。
- 补充 United 2025-09-30 和 JetBlue 2025-03-31 的契约遵守一手证据，均落在对应随访期内，保留为点时事实，不扩大成完整区间证明。
- 新观察输入文件与 clean-v1 逐字节相同，行业和随访资料仅保存在侧表；结构检查仍为 0 个错误。
- 逐日风险等级和评分侧表入口仍待完成。未运行付费模型评测。
