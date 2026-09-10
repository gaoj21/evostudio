# Credit Risk Monitoring 数据集：构建方法与当前数据说明

记录日期：2026-09-09；划分更新：2026-09-10。本文以实际发布文件和构建脚本为依据，说明当前 100 家公司版本，而不是早期 41 家 contemporary 版本。

## 1. 任务定义与评测单位

目标是观察一家公司的 trajectory：按时间先后向 agent 提供新闻和公司申报，让它积累证据，判断能否在风险事件发生前给出有依据的预警。评测单位是**公司的一段时间窗口**，不是单篇新闻，也不是每天一个风险分类题。

同一公司可以有多个窗口，因此公司数不等于轨迹数。窗口内某一天有新材料，才生成一次 observation；没有新材料的日期不补空记录。连续执行这些观察，才形成 memory 的累积过程。

当前任务**不要求逐日 low / medium / high / critical 风险标签**。只有评估逐日风险分级准确率才需要这类标签。主任务需要公司身份、按公开日期排列的证据、独立保存的事件类型/日期/主体，以及明确的预警判定规则。

预期比较内容如下，属于实验方案，不代表已有实验结果：

- 事件发现率：在符合条件的事件前轨迹中，是否出现有证据支撑的有效预警。
- 提前量：事件日期减去首次有效预警日期，按自然日计算。没有预警的轨迹保留为漏检，不能从分母删除。
- 证据可靠性：预警依据是否在当时可获得，是否指向对应公司和对应风险。
- Memory 增益：同一轨迹、相同输入和模型配置，比较开启与关闭 memory 的发现时间、证据串联和重复告警。
- 状态更新：融资、债务交换或重组退出后，是否及时修正旧判断；这与预测破产是不同测试。

“始终报高风险”不能直接算有效提前预警。有效预警的证据要求、目标事件范围和首次预警规则，需要在正式比较前固定。

## 2. 当前发布位置与统计口径

当前版本：`projects/credit_risk/dataset/expansion/releases/2026-09-10-random-dev-test-v1/`。

2026-09-10 按用户确认改为随机 dev/test：以固定 seed=42 从 100 家公司随机抽取 40 家作 test，其余 60 家作 dev；如果抽样拆开共享证据组，则重新抽样，使用首次满足隔离要求的结果。不再固定旧审阅数据或原 test，不按标签、观察数、行业优化划分，也不挑选分数或数量更好看的种子。结果：dev 为 60 家公司 / 64 条轨迹 / 1445 次观察，test 为 40 家公司 / 43 条轨迹 / 707 次观察。6:4 指公司数，轨迹和观察数自然随公司分配。同一公司所有轨迹、同一轨迹所有观察日保留在同一集合。材料、结局和观察内容不变；outcomes 仍单独保存，不能作为模型输入。

该版本由 outcomes-v2 经正文清洗形成 clean-v1，再附加 SEC 行业侧表形成 industry-v1，最后由 R16 统一 dev/test 名称，R17 曾保留旧分区约束，当前 R18 已取消该约束并按公司数随机划分 6:4。没有覆盖默认 live 数据集 `projects/credit_risk/dataset/contemporary/`。Feed、Batch run、Evolve 已加入版本与 split 选择；未指定版本的旧配置继续使用 contemporary。

本文表格统计从当前 `cases.jsonl`、`outcomes.jsonl`、`observations.jsonl`、`partitions.jsonl` 和 `industry_annotations.json` 重新计算。区分：

- 公司：唯一 CIK。
- 窗口/轨迹：唯一 case_id。
- 独立材料：唯一 document_id。
- 材料引用：每个窗口中出现的一份材料；同一材料在多个窗口出现会重复计数。
- 观察：一个窗口内有新材料的一个公开日期。

## 3. 数据来源

### 3.1 历史数据与 contemporary 的关系

早期 v0.x 使用 FNSPID 新闻标题、URL、媒体和日期，配合 SEC 事件。该新闻源主要用于较早年份，许多旧正文链接失效，不能视为当前完整正文来源。早期风险银标也不自动继承为当前任务答案。

contemporary 转向 GDELT 发现新闻，再抓取出版方正文，同时使用 SEC EDGAR 8-K 公司披露。当前扩展版本主要复用其候选公司缓存和原样本，再加入定向网页检索与经来源核对的少量摘要。

### 3.2 新闻发现与正文

`build_r02_fetch_news.py` 以公司 query_name 搜索 GDELT DOC 2.0，按月获取，达到返回上限时进一步拆分时间段；限制英语，做标题实体过滤，按日期和归一化标题去重。旧流程有至少 20 条新闻的密度门槛，也有每公司请求预算，因此缓存不是完整新闻全集。

`build_r05_fetch_fulltext.py` 从已发现 URL 抓出版方页面，用 trafilatura 提取正文，记录抓取状态和长度。抓取正文最多保存 15,000 字符。网页能访问、正文够长，并不证明抓到的是原文，也不证明该正文在历史日期就是这个版本。

### 3.3 公司申报

`build_r03_fetch_8k.py` 获取窗口内 8-K / 8-K/A，解析完整提交包中的对应正文，提取 Item 标题，保存申报日、accession、表单类型和正文。原抓取正文上限为 30,000 字符。

SEC 事件候选依赖 Item 1.03 等线索，但最终核验不能只靠关键词：借款合同可能引用该条款，主体也可能只有子公司。法律事件发生日与 SEC 文件提交日必须分开。

### 3.4 网页检索扩展

`expansion/web_sources.json` 保存覆盖 100 家公司的 479 个候选来源记录。它是发现清单，**不等于新增 479 篇已核验正文**。未经内容和发布日期核对的链接不进入模型 observation。

真正进入当前数据的人工一手来源摘要只有 13 份，明确标记为 `assistant_paraphrase_of_primary_source`，不是原始新闻全文。来源包括公司披露和 SEC 附件；必须匹配公司、确认公开日并落在窗口内。

### 3.5 SEC 行业元数据与随访目录

`build_r14_metadata.py` 从 SEC submissions JSON 获取 CIK、公司名、SIC 和 SIC 描述，校验 CIK，一次下载后缓存。当前 100 家均获取成功。

行业是抓取时的注册目录信息，用于分析数据组成；不能把当前行业分类冒充历史时点已知特征，因此只附加在侧表中。

`build_r15_followup.py` 根据旧负样本各自的 90 天随访区间，筛选 SEC recent 和相交的历史归档目录，按 accession 去重，整理正文审阅入口。目录没有目标条目，不等于现实世界不存在违约。

## 4. 构建过程与关键决策

### 4.1 候选公司、缓存复用与扩展

R10 读取 contemporary 的 candidates.csv、原 samples.jsonl、raw/news、raw/fulltext、raw/filings，以及事件核验和人工补充清单。它合并同公司已有材料，避免重新向慢速 API 获取已存在数据。

旧版本有 41 家公司；当前保留 39 个 legacy 窗口，另有 59 个 recovered_cache 窗口和 9 个 curated_monitoring 窗口。原公司并非机械地全部保留；没有可用、可定日材料的候选会排除，具体清单在 manifest 的 excluded_companies 和 inventory 中。

当前不再强制每家公司至少 20 条新闻：有真实申报材料的公司也可以进入。这样降低对新闻高曝光公司的偏好，但引入了稀疏轨迹，需要分层报告。

### 4.2 实体匹配

R09 的规则对公司名做规范化，并使用公司核心名、query_name 和少数容易冲突公司的专门别名。股票代码只有在 `$`、NASDAQ、NYSE 等明确金融语境中才匹配，避免把普通英文单词误当代码。

匹配检查使用标题与正文前 500 字符。Nikola 与体育人物名字碰撞有专门处理。规则输出 candidate_match、needs_review 或 off_target；R10 输入仅保留 candidate_match。

candidate_match 只表示有实体线索，不代表该公司是文章主角，也不代表具有信用风险相关性。律师招揽、诉讼广告、股价评论等仍可能存在。没有命中别名也可能误排真实材料，因此被排除的记录保留审计痕迹。

### 4.3 去重与正文隔离

同窗口按归一化标题去重，重复标题优先保留较早日期。这不是完整的语义去重，改写标题的重复报道可能仍保留。

进一步检查跨公司完全相同的非空正文，人工确认了 8 个污染正文哈希，影响 238 份独立新闻材料：通用行情首页、访问限制、导航、网站关闭公告、cookie 页面和验证码。

R13 按去除首尾空白后的完整正文 SHA256 精确匹配这些规则，将对应新闻正文清空并降级为 headline_only，保留标题、日期、URL、document_id、污染原因和原正文哈希。原文仍在父版本中，没有把污染文本改写成看似真实的摘要。

清洗后剩余 3 个跨公司相同正文簇属于多公司报道，保留。该检查只覆盖已发现的精确重复正文，不证明所有单篇噪声已清除。

### 4.4 事件核验与窗口修正

原正样本中有些日期是申报日期、后续文件日期或不同法律程序日期。人工依据一手披露核对事件类型、实际日期、主体及来源，存入 `review/event_reviews.json` 和 `review/outcome_audit_2026-09-09.json`。

一轮 88 窗口审阅中，64 个原正样本完成事件核验，其中 40 个日期得到修正；24 个原负样本证据不足，仍为 unverified。加上此前已核验内容，当前共 83 个窗口具有事件类型记录，但并非 83 个同质“公司破产正例”。

已核验 cached 窗口以事件前一天为结束日，向前取含首尾的 180 天。没有新核验日期时，优先保留原来已校正的窗口，避免重新引入旧错误日期。日期移动后重新筛材料；不能沿用落在新窗口之外的旧证据。

NaturalShrimp 和原 Big Lots 日期修正后缓存不足，分别补入 2024-08-14 和 2024-06-06 一手披露摘要。它们仍然只有一份材料，不能把“保留公司”误写成“补齐完整轨迹”。

### 4.5 状态变化窗口

9 个 curated_monitoring 窗口补充了再融资、债务交换、流动性更新、评级变化、债务加速及破产退出等情境。

这些窗口结束于所选披露公开日，可以包含事件当日披露，因此适合测试识别和状态更新，**不能与严格事件前窗口合并计算提前预测成绩**。例如 Vertex 的窗口包含进入 Chapter 11 到退出重组的材料，适合观察 memory 是否更新。

### 4.6 行业补齐

当前行业分类以 SEC SIC 为依据，保留原始代码与描述，再映射为 8 个本数据实际覆盖的粗粒度组。该映射是 SIC 范围分组，不是 GICS；生物医药等在此口径下属于制造业。因此粗粒度“制造业占比高”需要结合 58 个 SIC 代码理解。

行业缺失窗口由 92 降到 0，只是补齐了描述字段，没有重新抽样，也没有让样本变得均衡。

## 5. 时间组织、模型输入与防泄漏

`cases.jsonl` 保存整个窗口及全部材料，供构建和审计；它含完整窗口边界，不能直接一次性发送给 agent。

`observations.jsonl` 才是顺序输入基础。同窗口按 available_at 排序，每条只包含当天新增材料。输入中的 window_end 等于当前 as_of，而不是最终事件前窗口终点；sample_id 使用不直接编码标签的 case_id。

新闻 observation 最多使用正文前 3,000 字符。申报材料通过 Item 标题附近的抽取式片段呈现，默认文本预算 5,000 字符，保留字符位置说明。agent 实际看到的片段不等于 cases 中完整缓存正文，关键事实可能未进入片段，实验解释时必须考虑。

事件结局、原始标签、事件核验意见及当前行业数据只在侧表，不写入 observation。人工事件来源可能晚于事件本身，这种文件可以证明结局，却不能反向作为事前输入。

当前结构检查发现 0 个日期/引用错误，检查包括材料是否在窗口内、观察日是否与新增证据日期一致，以及若干结局字段是否进入 observation。它不是完整泄漏认证：

- 日期主要精确到天，不能判断同一天内先后顺序。
- 历史网页正文修订时间未逐条验证。
- first_public_at 尚未逐条核验，多数相关字段保持 null。
- 已公开的早期程序可能出现在较晚法律事件前，例如强制申请早于救济令，不能声称是在预测首次风险出现。

## 6. 分组与独立性

最初按 CIK 分组，避免同公司多个窗口跨集合。R13 进一步用共同 document_id 或相同非空正文连接公司，取传递闭包，防止同一报道跨 development / holdout。

若连通分组中有 legacy_review，整个分组归 review；否则只要有 development_candidate，整个分组归 development；其余才保留 holdout_candidate。空正文不会因为文本相同把无关公司连起来，但相同 document_id 仍会连接。

上述优先级是历史版本规则。当前 R18 不保留历史分区归属，随机按公司数划分 6:4，仅保留公司和共享证据组不可拆分的约束。分区名称统一并不代表事件与评测资格全部核验完成。共同股权控制、集团程序、重复法律事件没有全部通过该算法消除，例如 Twin Hospitality 与 FAT Brands 仍需按共同事件检查。共享证据分组也不等于行业分层或按时间留出。

## 7. 负样本随访及可评分边界

24 个历史负样本设置为窗口结束后的 90 天随访：起点为结束日加 1，终点为结束日加 90。目标限定为注册主体法律破产或已确认付款违约；常规再融资、评级变化或仅子公司破产不自动归入同一目标。

已整理 768 份区间内申报，含 75 份 8-K、12 份 10-K、11 份 10-Q、11 份 6-K；其余有大量持股或证券申报。没有发现 items 标记含 1.03 / 2.04 的申报，24 个区间均未超过本次核查日期，目录归档读取无错误。

这只是目录级筛查，尚非全部正文和法院/债权人信息的完整审阅。因此全部仍为 negative_verified=false。United 的 2025-09-30、JetBlue 的 2025-03-31 契约遵守披露作为区间内点时证据保存，不能扩大解释为整个区间没有风险。

这些轨迹可以用来观察行为，但暂不纳入可靠误报率结论。即使日后确认没有发生目标事件，也不等于期间所有高风险告警都是错误的；误报口径必须与告警声明的目标一致。

## 8. 文件结构与字段说明

| 文件 | 内容与用途 | 是否可直接作为 agent 输入 |
|---|---|---|
| cases.jsonl | 身份、窗口、cohort、完整文档列表 | 否，包含全窗口材料 |
| observations.jsonl | 当日新增材料与当前观察日 | 可，须按轨迹顺序执行 |
| curated_observations.jsonl | 9 个状态变化窗口的观察子集 | 可，但不作为纯事前预测集 |
| outcomes.jsonl | 原结局、核验事件、来源、主体、行业、审阅状态 | 否，仅评测侧表 |
| partitions.jsonl | 窗口的证据分组与候选分区 | 否，仅调度/统计 |
| obligors.csv | CIK、公司名、代码和查询名 | 公司注册信息，不含结局 |
| inventory.jsonl | 候选入选、排除及证据数量 | 否，构建审计 |
| excluded_news.jsonl | 实体待审、错配和重复标题排除记录 | 否，清洗审计 |
| backfill_queue.jsonl | 材料稀疏、覆盖缺口及查询提示 | 否，补充待办 |
| web_sources.json | 网页搜索发现记录 | 否，链接不等于证据 |
| review_backfill.json | 定向补充的一手摘要 | 经构建后才进入输入 |
| body_quality_rules.json | 精确正文污染哈希和原因 | 否，清洗规则 |
| industry_annotations.json | CIK、SIC、来源及当前分类口径 | 否，描述性侧表 |
| manifest.json | 数量、版本、输入输出哈希、父版本 | 否，复现审计 |

文档字段包括 document_id、available_at、kind、title、text、source_url、representation、temporal_status；申报额外有 accession/items，隔离正文额外记录哈希及原因。

观察字段包括 case_id/sample_id、company、cik、as_of、window_start、window_end、news_batch、filing_batch、document_ids。不同轨迹之间必须重置/隔离 memory，同一轨迹保持日期顺序，不能把 observation 全部独立打乱运行。

## 9. 版本和脚本索引

| 阶段 | 脚本/材料 | 作用 |
|---|---|---|
| 历史 v0.x | build_01 至 build_06 | SEC 事件、FNSPID 匹配、历史组装和银标，非当前主版本 |
| contemporary | build_r01 至 build_r05 | 近期候选、GDELT、8-K、样本组装、网页正文 |
| 早期修复 | build_r06 / r07 / r08 | 新闻质量、重新划分、日期纠正 |
| 可审计审阅 | build_r09_review.py | 候选实体规则、输入/结局隔离 |
| 100 家扩展 | build_r10_expand.py | 缓存复用、人工事件和摘要、观察构建 |
| 事件核验 | review/event_reviews.json、outcome_audit_2026-09-09.json | 法律事件日期、主体与来源 |
| 准备度审计 | build_r12_readiness.py | 日期和引用检查、旧标签待办 |
| 正文清洗 | build_r13_clean.py | 238 份正文隔离，共享材料分组 |
| 行业分类 | build_r14_metadata.py | SEC 目录缓存、行业侧表和新发布 |
| 随访资料包 | build_r15_followup.py | 24 个区间的申报索引 |
| dev/test 划分 | build_r16_dev_test.py | 合并开发分区、保留证据隔离、导出两份数据 |
| 历史 6:4 划分 | build_r17_split_60_40.py | 固定旧审阅组和原 test，已被 R18 替代 |
| 当前随机 6:4 | build_r18_random_split.py | seed=42，公司随机分配，保持共享证据组完整 |

主要不可变发布链：`2026-09-09-outcomes-v2` → `2026-09-09-clean-v1` → `2026-09-09-industry-v1` → `2026-09-10-dev-test-v1` → `2026-09-10-dev-test-60-40-v1` → `2026-09-10-random-dev-test-v1`。旧 web-v3、outcomes-v1 等目录属于历史中间版本，不混用它们的数量作为当前统计。

## 10. 复现方式

在项目根目录执行。输出目录必须使用新名称；清洗和行业发布会拒绝覆盖非空目录。原始缓存体积大，通常被 gitignore 排除；仅拿到 Git 仓库不保证拥有重建所需数据。

```sh
# 1. 从已有 contemporary 缓存及审核清单重建扩展版
python projects/credit_risk/dataset/builders/build_r10_expand.py \
  --discovery projects/credit_risk/dataset/expansion/web_sources.json \
  --evidence projects/credit_risk/dataset/expansion/review_backfill.json \
  --audit projects/credit_risk/dataset/review/outcome_audit_2026-09-09.json \
  --output projects/credit_risk/dataset/expansion/releases/NEW_OUTCOMES

# 2. 按人工正文哈希规则清洗，并重建观察和分组
python projects/credit_risk/dataset/builders/build_r13_clean.py \
  --source projects/credit_risk/dataset/expansion/releases/NEW_OUTCOMES \
  --output projects/credit_risk/dataset/expansion/releases/NEW_CLEAN \
  --rules projects/credit_risk/dataset/review/body_quality_2026-09-09.json

# 3. 使用已保存的行业注释，离线发布；不必重新联网获取 SEC
python - <<'PYCODE'
import sys
sys.path.insert(0, 'projects/credit_risk/dataset/builders')
from build_r14_metadata import publish
publish('projects/credit_risk/dataset/expansion/releases/NEW_CLEAN',
        'projects/credit_risk/dataset/review/industry_annotations_2026-09-09.json',
        'projects/credit_risk/dataset/expansion/releases/NEW_INDUSTRY')
PYCODE

# 4. 结构审计和代码测试
python projects/credit_risk/dataset/builders/build_r12_readiness.py \
  --release projects/credit_risk/dataset/expansion/releases/NEW_INDUSTRY \
  --output projects/credit_risk/dataset/review/releases/NEW_READINESS
python -m pytest tests/dataset tests/studio/test_evaluation_report.py -q
```

行业需要刷新时，用 R14 的 --release、--output 命令获取新缓存；随访构建则使用 R15 的 --checks、--metadata、--output。刷新后的当前 SEC 信息可能变化，因此论文或可复现实验应固定缓存与来源清单，而不是每次联网替换。

## 11. 当前可用范围与未完成事项

已具备首轮 trajectory 实验的数据基础，但不能把全体 107 个窗口不加区分地作为提前预警金标准。

- 先从已核验结局、主体明确、有多个观察日的事前窗口中确定实验清单。
- 9 个 curated 窗口单独用于状态更新；单观察日或极少材料窗口单列，避免用于强 memory 增益结论。
- 固定事件类型和法律阶段，避免把“子公司事件”“后续救济令”“退出重组”混算为首次破产。
- 接好 observation 与 outcomes 的评分侧表关联，确保结局不经过模型输入；新版 Evolve 已按 case_id 单独关联可用的已核验事件标签，但仍是窗口快照评分；完整 trajectory 评估尚未接入。普通批次不能从无标签的 sample_json 推算负标签。
- 固定同公司轨迹顺序和 memory 隔离策略，memory on/off 保持其他条件一致。
- 尚未完整核验的负样本不报告正式误报率。

旧构建文件仍有 risk_level=null、gold_eligible=false、gold_samples=0，以及 point_in_time_risk_unlabelled 等旧阻塞字段。R12 仍生成 2152 条风险标注待办并给出 full_evaluation_ready=false。这些沿用的是之前的逐日分级评测口径，**不表示当前 trajectory 任务必须补 2152 个风险标签**，也不应反过来把这些字段直接改成 true 就声称已完成新的验收。应为 trajectory 任务单独确定资格与评分配置。

本文编写前最近一次相关测试为 31 项通过。结构检查和单元测试不能证明新闻内容全部正确；本轮没有启动付费模型的完整实验，没有产生模型发现率或 memory 增益结论。

## 12. 数据统计与完整公司目录

以下表格按当前发布文件自动汇总。公司名称保留数据内写法；同一 CIK 的别名不额外计为公司。

### 12.1 总体规模

| 指标 | 当前值 |
| --- | --- |
| 公司 / 轨迹 / 观察 | 100 / 107 / 2152 |
| 独立材料 / 窗口材料引用 | 4427 / 4469 |
| 名义窗口总跨度 | 2024-01-25 至 2026-08-01 |
| 实际材料日期总跨度 | 2024-06-06 至 2026-07-29 |
| 有事件类型记录 / 结局未核验 | 83 / 24 |
| 少于 3 份材料的窗口 | 21 |
| 只有 1 个观察日的窗口 | 11 |
| 每窗口材料数：最小 / 中位 / 最大 | 1 / 8 / 746 |
| 每窗口观察数：最小 / 中位 / 最大 | 1 / 8 / 167 |

名义窗口跨度包含没有材料的日期；不代表每家公司覆盖整个总时间跨度。多数窗口很稀疏，整体 2152 次观察不能替代逐轨迹覆盖检查。

### 12.2 材料形式

| representation | 引用数 | 占全部引用 |
| --- | --- | --- |
| cached_article | 1912 | 42.8% |
| headline_only | 1903 | 42.6% |
| cached_filing_body | 641 | 14.3% |
| assistant_paraphrase_of_primary_source | 13 | 0.3% |

13 份摘要计入独立材料；正文隔离的 238 份已包含在 headline_only 中，不能再加一次。

### 12.3 轨迹来源与候选分区

| cohort | 窗口数 |
| --- | --- |
| legacy | 39 |
| recovered_cache | 59 |
| curated_monitoring | 9 |

| partition | 窗口数 |
| --- | --- |
| dev | 64 |
| test | 43 |

### 12.4 事件类型（按窗口计数）

| 事件类型 | 窗口数 |
| --- | --- |
| chapter_11 | 52 |
| unverified | 24 |
| chapter_7 | 9 |
| assignment_for_benefit_of_creditors | 3 |
| ccaa_restructuring | 3 |
| subsidiary_chapter_11 | 2 |
| refinancing | 2 |
| bankruptcy_emergence | 2 |
| receivership_order | 1 |
| receivership_application | 1 |
| canadian_bankruptcy_assignment | 1 |
| involuntary_chapter_7_order_for_relief | 1 |
| irish_winding_up_petition | 1 |
| debt_exchange | 1 |
| liquidity_update | 1 |
| rating_upgrade | 1 |
| negative_creditwatch | 1 |
| debt_acceleration | 1 |

### 12.5 行业构成（按唯一公司计数）

| SEC SIC 粗分组 | 公司数 |
| --- | --- |
| manufacturing | 45 |
| services | 15 |
| retail | 15 |
| transport_communications_utilities | 12 |
| finance_insurance_real_estate | 7 |
| agriculture | 3 |
| mining_oil_gas | 2 |
| wholesale | 1 |

原始 SIC 代码共 58 种。

### 12.6 排除与污染审计

| 排除原因 | 记录数 |
| --- | --- |
| duplicate_title | 292 |
| needs_review | 455 |
| off_target | 48 |

以上是构建排除记录数，可能涉及同一材料在不同窗口的引用，不应解释为独立噪声文章总数。238 份正文隔离另计，属于保留标题而移除正文的降级操作。

### 12.7 100 家公司完整目录

| 公司 | CIK | SIC | SEC 行业描述 | 窗口 | 材料引用 | 观察 |
| --- | --- | --- | --- | --- | --- | --- |
| 23andMe Holding Co. | 1804591 | 2834 | Pharmaceutical Preparations | 1 | 163 | 67 |
| 4Front Ventures Corp. | 1783875 | 2833 | Medicinal Chemicals & Botanical Products | 1 | 8 | 7 |
| Accelerate Diagnostics, Inc | 727207 | 3826 | Laboratory Analytical Instruments | 1 | 4 | 4 |
| Airbnb, Inc. | 1559720 | 7340 | Services-To Dwellings & Other Buildings | 1 | 3 | 3 |
| AMC Entertainment Holdings, Inc. | 1411579 | 7830 | Services-Motion Picture Theaters | 3 | 9 | 9 |
| Arch Therapeutics, Inc. | 1537561 | 3841 | Surgical & Medical Instruments & Apparatus | 1 | 2 | 2 |
| Avinger Inc | 1506928 | 3841 | Surgical & Medical Instruments & Apparatus | 1 | 28 | 26 |
| Benson Hill, Inc. | 1830210 | 2000 | Food and Kindred Products | 1 | 4 | 4 |
| BEYOND MEAT, INC. | 1655210 | 2000 | Food and Kindred Products | 1 | 44 | 27 |
| BioNTech SE | 1776985 | 2836 | Biological Products, (No Diagnostic Substances) | 1 | 91 | 51 |
| Bitcoin Depot Inc. | 1901799 | 6199 | Finance Services | 1 | 48 | 37 |
| Boeing Co. | 12927 | 3721 | Aircraft | 1 | 1 | 1 |
| Bright Green Corp | 1886799 | 2833 | Medicinal Chemicals & Botanical Products | 1 | 3 | 3 |
| Broad Street Realty, Inc. | 764897 | 6500 | Real Estate | 1 | 2 | 2 |
| BurgerFi International, Inc. | 1723580 | 5812 | Retail-Eating  Places | 1 | 11 | 8 |
| Cannabist Co Holdings Inc. | 1776738 | 0100 | Agricultural Production-Crops | 1 | 17 | 17 |
| Canoo Inc. | 1750153 | 3714 | Motor Vehicle Parts & Accessories | 1 | 73 | 48 |
| CareMax, Inc. | 1813914 | 8050 | Services-Nursing & Personal Care Facilities | 1 | 15 | 15 |
| Carvana Co. | 1690820 | 5500 | Retail-Auto Dealers & Gasoline Stations | 1 | 1 | 1 |
| CHARLES & COLVARD LTD | 1015155 | 3910 | Jewelry, Silverware & Plated Ware | 1 | 5 | 5 |
| Clearside Biomedical, Inc. | 1539029 | 2834 | Pharmaceutical Preparations | 1 | 8 | 7 |
| Container Store Group, Inc. | 1411688 | 5700 | Retail-Home Furniture, Furnishings & Equipment Stores | 1 | 37 | 27 |
| CUMULUS MEDIA INC | 1058623 | 4832 | Radio Broadcasting Stations | 1 | 2 | 2 |
| CUTERA INC | 1162461 | 3845 | Electromedical & Electrotherapeutic Apparatus | 1 | 5 | 5 |
| Danimer Scientific, Inc. | 1779020 | 2821 | Plastic Materials, Synth Resins & Nonvulcan Elastomers | 1 | 11 | 11 |
| DIAMONDHEAD CASINO CORP | 844887 | 7011 | Hotels & Motels | 1 | 1 | 1 |
| Dine Brands Global, Inc. | 49754 | 5812 | Retail-Eating  Places | 1 | 29 | 21 |
| DYNATRONICS CORP | 720875 | 3841 | Surgical & Medical Instruments & Apparatus | 1 | 1 | 1 |
| DZS INC. | 1101680 | 3661 | Telephone & Telegraph Apparatus | 1 | 9 | 8 |
| EchoStar CORP | 1415404 | 4899 | Communications Services, NEC | 1 | 77 | 52 |
| ENGLOBAL CORP | 933738 | 8711 | Services-Engineering Services | 1 | 7 | 7 |
| FAT Brands Inc. | 1705012 | 5812 | Retail-Eating  Places | 2 | 64 | 44 |
| FORMER BL STORES INC | 768835 | 5331 | Retail-Variety Stores | 1 | 1 | 1 |
| Frontier Group Holdings, Inc. | 1670076 | 4512 | Air Transportation, Scheduled | 1 | 75 | 38 |
| GameStop Corp. | 1326380 | 5734 | Retail-Computer & Computer Software Stores | 1 | 1 | 1 |
| Gaucho Group Holdings, Inc. | 1559998 | 6552 | Land Subdividers & Developers (No Cemeteries) | 1 | 11 | 7 |
| Global Clean Energy Holdings, Inc. | 748790 | 2860 | Industrial Organic Chemicals | 1 | 37 | 30 |
| GoHealth, Inc. | 1808220 | 6411 | Insurance Agents, Brokers & Service | 1 | 2 | 2 |
| Gold Flora Corp. | 1876945 | 0100 | Agricultural Production-Crops | 1 | 6 | 6 |
| Gritstone bio, Inc. | 1656634 | 2836 | Biological Products, (No Diagnostic Substances) | 1 | 47 | 28 |
| Hilton Worldwide Holdings Inc. | 1585689 | 7011 | Hotels & Motels | 1 | 5 | 5 |
| iCoreConnect Inc. | 1906133 | 7372 | Services-Prepackaged Software | 1 | 12 | 12 |
| iHeartMedia, Inc. | 1400891 | 4832 | Radio Broadcasting Stations | 2 | 4 | 4 |
| iLearningEngines, Inc. | 1835972 | 7372 | Services-Prepackaged Software | 1 | 104 | 63 |
| Independence Contract Drilling, Inc. | 1537028 | 1381 | Drilling Oil & Gas Wells | 1 | 5 | 5 |
| Inotiv, Inc. | 720154 | 8731 | Services-Commercial Physical & Biological Research | 1 | 2 | 2 |
| IntelGenx Technologies Corp. | 1098880 | 2834 | Pharmaceutical Preparations | 1 | 1 | 1 |
| IO Biotech, Inc. | 1865494 | 2834 | Pharmaceutical Preparations | 1 | 7 | 7 |
| IROBOT CORP | 1159167 | 3630 | Household Appliances | 1 | 122 | 74 |
| Iterum Therapeutics plc | 1659323 | 2834 | Pharmaceutical Preparations | 1 | 2 | 2 |
| JetBlue Airways Corporation | 1158463 | 4512 | Air Transportation, Scheduled | 2 | 9 | 8 |
| Kiromic Biopharma, Inc. | 1792581 | 2836 | Biological Products, (No Diagnostic Substances) | 1 | 6 | 5 |
| KOHLS Corp | 885639 | 5311 | Retail-Department Stores | 1 | 5 | 5 |
| LadRx Corp | 799698 | 2836 | Biological Products, (No Diagnostic Substances) | 1 | 2 | 2 |
| Lazydays Holdings, Inc. | 1721741 | 5500 | Retail-Auto Dealers & Gasoline Stations | 1 | 16 | 13 |
| Li-Cycle Holdings Corp. | 1828811 | 4955 | Hazardous Waste Management | 1 | 34 | 28 |
| LIPELLA PHARMACEUTICALS INC. | 1347242 | 2834 | Pharmaceutical Preparations | 1 | 3 | 3 |
| Loop Media, Inc. | 1643988 | 7363 | Services-Help Supply Services | 1 | 4 | 4 |
| Lucid Group, Inc. | 1811210 | 3711 | Motor Vehicles & Passenger Car Bodies | 1 | 104 | 55 |
| Luminar Technologies, Inc./DE | 1758057 | 3714 | Motor Vehicle Parts & Accessories | 1 | 62 | 47 |
| LUXURBAN HOTELS INC. | 1893311 | 6500 | Real Estate | 1 | 8 | 8 |
| Macy's, Inc. | 794367 | 5311 | Retail-Department Stores | 1 | 23 | 19 |
| MARA Holdings, Inc. | 1507605 | 6199 | Finance Services | 1 | 96 | 60 |
| MARIN SOFTWARE INC | 1389002 | 7374 | Services-Computer Processing & Data Preparation | 1 | 1 | 1 |
| Moderna, Inc. | 1682852 | 2836 | Biological Products, (No Diagnostic Substances) | 1 | 305 | 101 |
| ModivCare Inc | 1220754 | 4700 | Transportation Services | 1 | 67 | 40 |
| Molecular Templates, Inc. | 1183765 | 2834 | Pharmaceutical Preparations | 1 | 6 | 5 |
| Mondee Holdings, Inc. | 1828852 | 4700 | Transportation Services | 1 | 6 | 6 |
| NaturalShrimp Inc | 1465470 | 0900 | Fishing, Hunting and Trapping | 1 | 1 | 1 |
| Nikola Corp | 1731289 | 3711 | Motor Vehicles & Passenger Car Bodies | 1 | 15 | 12 |
| Nine Energy Service, Inc. | 1532286 | 1389 | Oil & Gas Field Services, NEC | 1 | 2 | 2 |
| OFFICE PROPERTIES INCOME TRUST | 1456772 | 6500 | Real Estate | 1 | 6 | 6 |
| OLENOX INDUSTRIES INC. | 1023994 | 5030 | Wholesale-Lumber & Other Construction Materials | 1 | 13 | 12 |
| Omega Therapeutics, Inc. | 1850838 | 2836 | Biological Products, (No Diagnostic Substances) | 1 | 9 | 9 |
| PFIZER INC | 78003 | 2834 | Pharmaceutical Preparations | 1 | 435 | 65 |
| PLUG POWER INC | 1093691 | 3620 | Electrical Industrial Apparatus | 1 | 142 | 94 |
| QVC Group, Inc. | 1355096 | 5961 | Retail-Catalog & Mail-Order Houses | 1 | 3 | 3 |
| Rivian Automotive, Inc. / DE | 1874178 | 3711 | Motor Vehicles & Passenger Car Bodies | 1 | 746 | 167 |
| SANGAMO THERAPEUTICS, INC | 1001233 | 2836 | Biological Products, (No Diagnostic Substances) | 1 | 4 | 3 |
| Sinclair, Inc. | 1971213 | 4833 | Television Broadcasting Stations | 1 | 5 | 5 |
| Sleep Number Corp | 827187 | 2510 | Household Furniture | 1 | 43 | 27 |
| SOCIETY PASS INCORPORATED. | 1817511 | 7389 | Services-Business Services, NEC | 1 | 9 | 9 |
| Sonder Holdings Inc. | 1819395 | 7000 | Hotels, Rooming Houses, Camps & Other Lodging Places | 1 | 94 | 28 |
| Spirit Airlines, Inc. | 1498710 | 4512 | Air Transportation, Scheduled | 1 | 154 | 41 |
| Sunnova Energy International Inc. | 1772695 | 4931 | Electric & Other Services Combined | 1 | 57 | 36 |
| Sunrun Inc. | 1469367 | 3690 | Miscellaneous Electrical Machinery, Equipment & Supplies | 1 | 95 | 60 |
| Tesla, Inc. | 1318605 | 3711 | Motor Vehicles & Passenger Car Bodies | 1 | 3 | 3 |
| TILT Holdings Inc. | 1761510 | 2111 | Cigarettes | 1 | 5 | 5 |
| TPI COMPOSITES, INC | 1455684 | 3510 | Engines & Turbines | 1 | 28 | 22 |
| Trinseo PLC | 1519061 | 2821 | Plastic Materials, Synth Resins & Nonvulcan Elastomers | 1 | 15 | 14 |
| Twin Hospitality Group Inc. | 2011954 | 5812 | Retail-Eating  Places | 1 | 5 | 5 |
| United Airlines Holdings, Inc. | 100517 | 4512 | Air Transportation, Scheduled | 1 | 220 | 67 |
| Vertex Energy Inc | 890447 | 2911 | Petroleum Refining | 2 | 18 | 18 |
| Vroom Inc | 1580864 | 5500 | Retail-Auto Dealers & Gasoline Stations | 2 | 12 | 12 |
| Wag! Group Co. | 1842356 | 7200 | Services-Personal Services | 1 | 8 | 8 |
| Wayfair Inc. | 1616707 | 5961 | Retail-Catalog & Mail-Order Houses | 1 | 174 | 92 |
| Wendy's Co | 30697 | 5810 | Retail-Eating & Drinking Places | 1 | 15 | 12 |
| WOLFSPEED, INC. | 895419 | 3674 | Semiconductors & Related Devices | 1 | 116 | 65 |
| WW INTERNATIONAL, INC. | 105319 | 7200 | Services-Personal Services | 1 | 22 | 18 |
| ZYNEX INC | 846475 | 3845 | Electromedical & Electrotherapeutic Apparatus | 1 | 11 | 10 |

### 12.8 107 条轨迹清单

| case_id | 公司 | 窗口 | 事件类型 | 事件日期 | 材料 | 观察 | cohort | partition |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 0f4dc83f7b66d30e3ecc | 23andMe Holding Co. | 2024-09-24 ～ 2025-03-22 | chapter_11 | 2025-03-23 | 163 | 67 | legacy | legacy_review |
| c05f4ed4b6cb0bb28402 | 4Front Ventures Corp. | 2024-12-11 ～ 2025-06-08 | canadian_bankruptcy_assignment | 2025-06-09 | 8 | 7 | recovered_cache | holdout_candidate |
| b7301cd3f64a78674e12 | Accelerate Diagnostics, Inc | 2024-11-09 ～ 2025-05-07 | chapter_11 | 2025-05-08 | 4 | 4 | recovered_cache | holdout_candidate |
| e0f6c01dcd97892f1bd9 | Airbnb, Inc. | 2025-06-18 ～ 2025-12-14 | unverified | 未核验 | 3 | 3 | recovered_cache | development_candidate |
| 419be6bf3851e5093fd9 | AMC Entertainment Holdings, Inc. | 2024-01-25 ～ 2024-07-22 | refinancing | 2024-07-22 | 1 | 1 | curated_monitoring | development_candidate |
| 228592a601fd138ffc0d | AMC ENTERTAINMENT HOLDINGS, INC. | 2024-08-01 ～ 2025-01-27 | unverified | 未核验 | 7 | 7 | recovered_cache | development_candidate |
| 9e66613414028b1e786d | AMC Entertainment Holdings, Inc. | 2025-01-27 ～ 2025-07-25 | refinancing | 2025-07-25 | 1 | 1 | curated_monitoring | development_candidate |
| ec92d02920fceedc3291 | Arch Therapeutics, Inc. | 2024-10-20 ～ 2025-04-17 | chapter_11 | 2025-04-18 | 2 | 2 | recovered_cache | development_candidate |
| 7027e65ba95ba562f274 | Avinger Inc | 2024-08-14 ～ 2025-02-09 | assignment_for_benefit_of_creditors | 2025-02-10 | 28 | 26 | legacy | legacy_review |
| 2754e79dd77a0d932f47 | Benson Hill, Inc. | 2024-09-21 ～ 2025-03-19 | chapter_11 | 2025-03-20 | 4 | 4 | recovered_cache | development_candidate |
| 6c304f83048cfca9a638 | BEYOND MEAT, INC. | 2024-08-23 ～ 2025-02-18 | unverified | 未核验 | 44 | 27 | legacy | legacy_review |
| 39df444782611ddcead9 | BioNTech SE | 2024-10-10 ～ 2025-04-07 | unverified | 未核验 | 91 | 51 | legacy | legacy_review |
| 6e1a26d96778402bbcdd | Bitcoin Depot Inc. | 2025-11-18 ～ 2026-05-16 | chapter_11 | 2026-05-17 | 48 | 37 | legacy | legacy_review |
| 28470b50ef17182e8a68 | Boeing Co. | 2024-05-06 ～ 2024-11-01 | negative_creditwatch | 2024-11-01 | 1 | 1 | curated_monitoring | development_candidate |
| fd83a8247f3c9a374785 | Bright Green Corp | 2024-08-26 ～ 2025-02-21 | chapter_11 | 2025-02-22 | 3 | 3 | recovered_cache | development_candidate |
| a6ef334fd1eba5e02014 | Broad Street Realty, Inc. | 2025-09-21 ～ 2026-03-19 | chapter_7 | 2026-03-20 | 2 | 2 | recovered_cache | development_candidate |
| 83c7ee1baab6512bedec | BurgerFi International, Inc. | 2024-03-15 ～ 2024-09-10 | chapter_11 | 2024-09-11 | 11 | 8 | legacy | legacy_review |
| ce66d025f6900e3f7c18 | Cannabist Co Holdings Inc. | 2025-09-25 ～ 2026-03-23 | ccaa_restructuring | 2026-03-24 | 17 | 17 | recovered_cache | development_candidate |
| 31a4865ed4fa9b7e8521 | Canoo Inc. | 2024-07-21 ～ 2025-01-16 | chapter_7 | 2025-01-17 | 73 | 48 | legacy | legacy_review |
| 4b8cd8152ac8651252b1 | CareMax, Inc. | 2024-05-21 ～ 2024-11-16 | chapter_11 | 2024-11-17 | 15 | 15 | recovered_cache | development_candidate |
| b240a50f184c33783bb7 | Carvana Co. | 2024-09-15 ～ 2025-03-13 | rating_upgrade | 2025-03-13 | 1 | 1 | curated_monitoring | development_candidate |
| fa53771360cd0642ff9e | CHARLES & COLVARD LTD | 2025-09-03 ～ 2026-03-01 | chapter_11 | 2026-03-02 | 5 | 5 | recovered_cache | development_candidate |
| 09c299a522cb533e80e4 | Clearside Biomedical, Inc. | 2025-05-27 ～ 2025-11-22 | chapter_11 | 2025-11-23 | 8 | 7 | recovered_cache | development_candidate |
| 8074b385e866503a5c47 | Container Store Group, Inc. | 2024-06-25 ～ 2024-12-21 | chapter_11 | 2024-12-22 | 37 | 27 | legacy | legacy_review |
| 6726a02f1061155bfbe6 | CUMULUS MEDIA INC | 2025-09-05 ～ 2026-03-03 | chapter_11 | 2026-03-04 | 2 | 2 | recovered_cache | development_candidate |
| 6c3e9dce13ce614344c2 | CUTERA INC | 2024-09-06 ～ 2025-03-04 | chapter_11 | 2025-03-05 | 5 | 5 | recovered_cache | holdout_candidate |
| 8c05ec25f8aff10f363f | Danimer Scientific, Inc. | 2024-09-19 ～ 2025-03-17 | chapter_11 | 2025-03-18 | 11 | 11 | recovered_cache | development_candidate |
| c716450a4ef80dd619d1 | DIAMONDHEAD CASINO CORP | 2025-02-01 ～ 2025-07-30 | involuntary_chapter_7_order_for_relief | 2025-07-31 | 1 | 1 | recovered_cache | development_candidate |
| f3a83b1e781751839f91 | Dine Brands Global, Inc. | 2024-11-08 ～ 2025-05-06 | unverified | 未核验 | 29 | 21 | legacy | legacy_review |
| 097190ddd0feed2ee257 | DYNATRONICS CORP | 2025-07-13 ～ 2026-01-08 | chapter_7 | 2026-01-09 | 1 | 1 | recovered_cache | development_candidate |
| da4b514dc9baf1b54559 | DZS INC. | 2024-09-15 ～ 2025-03-13 | chapter_7 | 2025-03-14 | 9 | 8 | recovered_cache | development_candidate |
| 5a74a77feb4654645287 | EchoStar CORP | 2026-02-03 ～ 2026-08-01 | subsidiary_chapter_11 | 2026-08-02 | 77 | 52 | legacy | legacy_review |
| aa40cc6e7e27485fa0d2 | ENGLOBAL CORP | 2024-09-06 ～ 2025-03-04 | chapter_11 | 2025-03-05 | 7 | 7 | recovered_cache | development_candidate |
| 62b5c0f6d189e10616e2 | FAT Brands Inc. | 2025-05-26 ～ 2025-11-21 | debt_acceleration | 2025-11-17 | 26 | 17 | curated_monitoring | legacy_review |
| 852eceb46aa26d796358 | Fat Brands, Inc | 2025-07-30 ～ 2026-01-25 | chapter_11 | 2026-01-26 | 38 | 27 | legacy | legacy_review |
| a2eb8c58281dbcecd4be | FORMER BL STORES INC | 2024-03-13 ～ 2024-09-08 | chapter_11 | 2024-09-09 | 1 | 1 | recovered_cache | development_candidate |
| 6098e4a01070422fa8b4 | Frontier Group Holdings, Inc. | 2025-05-11 ～ 2025-11-06 | unverified | 未核验 | 75 | 38 | legacy | legacy_review |
| a6ebe270180d1686ee24 | GameStop Corp. | 2025-11-19 ～ 2026-05-17 | unverified | 未核验 | 1 | 1 | recovered_cache | development_candidate |
| 933f3dfb774116c9f5e6 | Gaucho Group Holdings, Inc. | 2024-05-16 ～ 2024-11-11 | chapter_11 | 2024-11-12 | 11 | 7 | legacy | legacy_review |
| 13f3d97fe0b160f9fe33 | Global Clean Energy Holdings, Inc. | 2024-10-18 ～ 2025-04-15 | chapter_11 | 2025-04-16 | 37 | 30 | legacy | legacy_review |
| 71c886af99096af8215c | GoHealth, Inc. | 2025-12-09 ～ 2026-06-06 | chapter_11 | 2026-06-07 | 2 | 2 | recovered_cache | development_candidate |
| 07abffc1ffc74f44976a | Gold Flora Corp. | 2024-09-28 ～ 2025-03-26 | receivership_application | 2025-03-27 | 6 | 6 | recovered_cache | development_candidate |
| cec7c13a9a8d0075bde8 | Gritstone bio, Inc. | 2024-04-13 ～ 2024-10-09 | chapter_11 | 2024-10-10 | 47 | 28 | legacy | legacy_review |
| a86d0a7797832c426d1b | Hilton Worldwide Holdings Inc. | 2025-11-05 ～ 2026-05-03 | unverified | 未核验 | 5 | 5 | recovered_cache | development_candidate |
| f67e71315afd4f52a2c1 | iCoreConnect Inc. | 2024-12-04 ～ 2025-06-01 | chapter_11 | 2025-06-02 | 12 | 12 | recovered_cache | development_candidate |
| 6a97e16ebedfe9ccbcb5 | iHeartMedia, Inc. | 2024-06-27 ～ 2024-12-23 | debt_exchange | 未核验 | 2 | 2 | curated_monitoring | development_candidate |
| c8825e987a2955a124db | iHeartMedia, Inc. | 2025-06-04 ～ 2025-11-30 | unverified | 未核验 | 2 | 2 | recovered_cache | development_candidate |
| eb7f7791800a14545fe1 | iLearningEngines, Inc. | 2024-06-23 ～ 2024-12-19 | chapter_11 | 2024-12-20 | 104 | 63 | legacy | legacy_review |
| a674128a23aa1b518ff3 | Independence Contract Drilling, Inc. | 2024-06-05 ～ 2024-12-01 | chapter_11 | 2024-12-02 | 5 | 5 | recovered_cache | development_candidate |
| cad79422b112fc2e5d4d | Inotiv, Inc. | 2025-12-05 ～ 2026-06-02 | chapter_11 | 2026-06-03 | 2 | 2 | recovered_cache | holdout_candidate |
| 58d74242a02e1f49c16b | IntelGenx Technologies Corp. | 2024-09-01 ～ 2025-02-27 | chapter_7 | 2025-02-28 | 1 | 1 | recovered_cache | development_candidate |
| 3f4c8e24e82afd14e02c | IO Biotech, Inc. | 2025-10-02 ～ 2026-03-30 | chapter_7 | 2026-03-31 | 7 | 7 | recovered_cache | development_candidate |
| 6c207a2f93e2e694da88 | IROBOT CORP | 2025-06-17 ～ 2025-12-13 | chapter_11 | 2025-12-14 | 122 | 74 | legacy | legacy_review |
| d28b9ca65c447dead5c6 | Iterum Therapeutics plc | 2025-09-28 ～ 2026-03-26 | irish_winding_up_petition | 2026-03-27 | 2 | 2 | recovered_cache | development_candidate |
| 8c45cff8ee16e915f7a4 | JETBLUE AIRWAYS CORP | 2024-09-07 ～ 2025-03-05 | unverified | 未核验 | 4 | 4 | recovered_cache | development_candidate |
| d518735f004bae2bbe34 | JetBlue Airways Corporation | 2024-08-02 ～ 2025-01-28 | liquidity_update | 未核验 | 5 | 4 | curated_monitoring | development_candidate |
| d9ecd2de1b974a136fb4 | Kiromic Biopharma, Inc. | 2024-09-22 ～ 2025-03-20 | chapter_7 | 2025-03-21 | 6 | 5 | recovered_cache | holdout_candidate |
| 7f0a8a0533298acf24e0 | KOHLS Corp | 2025-03-22 ～ 2025-09-17 | unverified | 未核验 | 5 | 5 | recovered_cache | development_candidate |
| 614c4e090369b193c3e3 | LadRx Corp | 2025-01-29 ～ 2025-07-27 | assignment_for_benefit_of_creditors | 2025-07-28 | 2 | 2 | recovered_cache | development_candidate |
| ed1495a54d0e32cbf443 | Lazydays Holdings, Inc. | 2025-06-01 ～ 2025-11-27 | assignment_for_benefit_of_creditors | 2025-11-28 | 16 | 13 | recovered_cache | holdout_candidate |
| 757e02ace7e1ce1286f3 | Li-Cycle Holdings Corp. | 2024-11-15 ～ 2025-05-13 | ccaa_restructuring | 2025-05-14 | 34 | 28 | legacy | legacy_review |
| 3f3ee0efc2db1761e157 | LIPELLA PHARMACEUTICALS INC. | 2025-10-01 ～ 2026-03-29 | chapter_11 | 2026-03-30 | 3 | 3 | recovered_cache | holdout_candidate |
| 66f56bb25c15be4f97bf | Loop Media, Inc. | 2025-04-12 ～ 2025-10-08 | chapter_7 | 2025-10-09 | 4 | 4 | recovered_cache | development_candidate |
| 42beca2a7f5b4d6fc013 | Lucid Group, Inc. | 2025-10-19 ～ 2026-04-16 | unverified | 未核验 | 104 | 55 | legacy | legacy_review |
| 4cd03ff797e3adae89ba | Luminar Technologies, Inc./DE | 2025-06-18 ～ 2025-12-14 | chapter_11 | 2025-12-15 | 62 | 47 | legacy | legacy_review |
| ee289335aaf9f3d9add5 | LUXURBAN HOTELS INC. | 2025-03-18 ～ 2025-09-13 | chapter_11 | 2025-09-14 | 8 | 8 | recovered_cache | holdout_candidate |
| 9c3a0d2356d88280bcfd | Macy's, Inc. | 2024-08-14 ～ 2025-02-09 | unverified | 未核验 | 23 | 19 | legacy | legacy_review |
| 511fb5657327ab8c6e9c | MARA Holdings, Inc. | 2024-08-14 ～ 2025-02-09 | unverified | 未核验 | 96 | 60 | legacy | legacy_review |
| eebf442ff7dd5752581c | MARIN SOFTWARE INC | 2025-01-02 ～ 2025-06-30 | chapter_11 | 2025-07-01 | 1 | 1 | recovered_cache | holdout_candidate |
| f78c6199d611575be5a0 | Moderna, Inc. | 2025-09-27 ～ 2026-03-25 | unverified | 未核验 | 305 | 101 | legacy | legacy_review |
| b5f3171418792b575bc9 | ModivCare Inc | 2025-02-21 ～ 2025-08-19 | chapter_11 | 2025-08-20 | 67 | 40 | legacy | legacy_review |
| abe8f0cde24bc837fb3b | Molecular Templates, Inc. | 2024-10-22 ～ 2025-04-19 | chapter_11 | 2025-04-20 | 6 | 5 | recovered_cache | development_candidate |
| 6f234dc0659e7f9e05a9 | Mondee Holdings, Inc. | 2024-07-18 ～ 2025-01-13 | chapter_11 | 2025-01-14 | 6 | 6 | recovered_cache | development_candidate |
| fe578abd3ce8142e8ac2 | NaturalShrimp Inc | 2024-03-13 ～ 2024-09-08 | receivership_order | 2024-09-09 | 1 | 1 | recovered_cache | development_candidate |
| 5992f69df19bfb06a809 | Nikola Corp | 2024-08-23 ～ 2025-02-18 | chapter_11 | 2025-02-19 | 15 | 12 | legacy | legacy_review |
| c1a07668fe34b04c0735 | Nine Energy Service, Inc. | 2025-08-05 ～ 2026-01-31 | chapter_11 | 2026-02-01 | 2 | 2 | recovered_cache | development_candidate |
| e449e2d91ed4df1fc263 | OFFICE PROPERTIES INCOME TRUST | 2025-05-03 ～ 2025-10-29 | chapter_11 | 2025-10-30 | 6 | 6 | recovered_cache | development_candidate |
| 44f9a29d1b432e7025a1 | OLENOX INDUSTRIES INC. | 2025-10-30 ～ 2026-04-27 | subsidiary_chapter_11 | 2026-04-28 | 13 | 12 | recovered_cache | development_candidate |
| 37b7fbb206f09eeb3e8f | Omega Therapeutics, Inc. | 2024-08-14 ～ 2025-02-09 | chapter_11 | 2025-02-10 | 9 | 9 | recovered_cache | development_candidate |
| 6eff43edea874e904543 | PFIZER INC | 2024-08-01 ～ 2025-01-27 | unverified | 未核验 | 435 | 65 | legacy | legacy_review |
| e75d9af50e7ff488660f | PLUG POWER INC | 2024-08-14 ～ 2025-02-09 | unverified | 未核验 | 142 | 94 | legacy | legacy_review |
| 139532f134f25b3242d0 | QVC Group, Inc. | 2025-10-18 ～ 2026-04-15 | chapter_11 | 2026-04-16 | 3 | 3 | recovered_cache | development_candidate |
| be53201edfa9468c1a9e | Rivian Automotive, Inc. / DE | 2024-08-07 ～ 2025-02-02 | unverified | 未核验 | 746 | 167 | legacy | legacy_review |
| 74d7b864e9b5aa4c2faf | SANGAMO THERAPEUTICS, INC | 2025-12-25 ～ 2026-06-22 | chapter_11 | 2026-06-23 | 4 | 3 | recovered_cache | development_candidate |
| 88e8dc3704b5d9f4ed76 | Sinclair, Inc. | 2025-06-18 ～ 2025-12-14 | unverified | 未核验 | 5 | 5 | recovered_cache | development_candidate |
| ae7f45e297d50b89ea57 | Sleep Number Corp | 2025-12-14 ～ 2026-06-11 | chapter_11 | 2026-06-12 | 43 | 27 | legacy | legacy_review |
| 37efa3814a991009e049 | SOCIETY PASS INCORPORATED. | 2025-11-13 ～ 2026-05-11 | chapter_11 | 2026-05-12 | 9 | 9 | recovered_cache | development_candidate |
| 74392ee3b5ff9fb25215 | Sonder Holdings Inc. | 2025-05-18 ～ 2025-11-13 | chapter_7 | 2025-11-14 | 94 | 28 | legacy | legacy_review |
| f14d80f34635b89c98da | Spirit Airlines, Inc. | 2024-05-22 ～ 2024-11-17 | chapter_11 | 2024-11-18 | 154 | 41 | legacy | legacy_review |
| f66e0d4aeab3a3713ab9 | Sunnova Energy International Inc. | 2024-12-10 ～ 2025-06-07 | chapter_11 | 2025-06-08 | 57 | 36 | legacy | legacy_review |
| 5e4d6814935c5d6f9a26 | Sunrun Inc. | 2024-07-19 ～ 2025-01-14 | unverified | 未核验 | 95 | 60 | legacy | legacy_review |
| d1388ce1c26f7f10f6a6 | Tesla, Inc. | 2024-07-26 ～ 2025-01-21 | unverified | 未核验 | 3 | 3 | recovered_cache | development_candidate |
| e2e96d25831161db8696 | TILT Holdings Inc. | 2025-05-11 ～ 2025-11-06 | ccaa_restructuring | 2025-11-07 | 5 | 5 | recovered_cache | development_candidate |
| 350ba664b3a8956adb51 | TPI COMPOSITES, INC | 2025-02-12 ～ 2025-08-10 | chapter_11 | 2025-08-11 | 28 | 22 | legacy | legacy_review |
| 7e9d212a3640239fc05d | Trinseo PLC | 2025-11-27 ～ 2026-05-25 | chapter_11 | 2026-05-26 | 15 | 14 | recovered_cache | development_candidate |
| 9e2a3970db6e7b603205 | Twin Hospitality Group Inc. | 2025-07-30 ～ 2026-01-25 | chapter_11 | 2026-01-26 | 5 | 5 | recovered_cache | development_candidate |
| e8eb47238305a0b1000e | United Airlines Holdings, Inc. | 2025-02-05 ～ 2025-08-03 | unverified | 未核验 | 220 | 67 | legacy | legacy_review |
| 80c207bb1f6416184d88 | Vertex Energy Inc | 2024-07-27 ～ 2025-01-22 | bankruptcy_emergence | 2025-01-22 | 13 | 13 | curated_monitoring | development_candidate |
| abb3ba16e076cd3fad38 | Vertex Energy Inc. | 2024-03-28 ～ 2024-09-23 | chapter_11 | 2024-09-24 | 5 | 5 | recovered_cache | development_candidate |
| 8504b64b461d0a72e9b0 | Vroom Inc | 2024-07-19 ～ 2025-01-14 | bankruptcy_emergence | 2025-01-14 | 8 | 8 | curated_monitoring | development_candidate |
| cc136338b29d3bb8e195 | Vroom, Inc. | 2024-05-17 ～ 2024-11-12 | chapter_11 | 2024-11-13 | 4 | 4 | recovered_cache | development_candidate |
| cb76b150b5fb8dfdee8d | Wag! Group Co. | 2025-01-22 ～ 2025-07-20 | chapter_11 | 2025-07-21 | 8 | 8 | recovered_cache | holdout_candidate |
| 6ed4e0b4e624a8296b33 | Wayfair Inc. | 2025-12-05 ～ 2026-06-02 | unverified | 未核验 | 174 | 92 | legacy | legacy_review |
| ab905feef08970a81e7b | Wendy's Co | 2024-08-02 ～ 2025-01-28 | unverified | 未核验 | 15 | 12 | legacy | legacy_review |
| 1be45d9c55d464eb8623 | WOLFSPEED, INC. | 2025-01-01 ～ 2025-06-29 | chapter_11 | 2025-06-30 | 116 | 65 | legacy | legacy_review |
| 79b96f73bb0c408ab314 | WW INTERNATIONAL, INC. | 2024-11-07 ～ 2025-05-05 | chapter_11 | 2025-05-06 | 22 | 18 | legacy | legacy_review |
| 016391a4afc5aa8d59d6 | ZYNEX INC | 2025-06-18 ～ 2025-12-14 | chapter_11 | 2025-12-15 | 11 | 10 | recovered_cache | development_candidate |

## 13. 审计来源与文件指纹

- [当前 manifest](../../dataset/expansion/releases/2026-09-09-industry-v1/manifest.json)
- [事件核验记录](../../dataset/review/OUTCOME_REVIEW_2026-09-09.md)
- [清洗与准备度记录](../../dataset/review/READINESS_2026-09-09.md)
- [行业来源注释](../../dataset/review/industry_annotations_2026-09-09.json)
- [负样本点时证据](../../dataset/review/followup_checks_2026-09-09.json)
- [24 个随访资料包](../../dataset/review/releases/2026-09-09-followup-v2/followup_packets.json)

| 当前发布文件 | SHA256 |
| --- | --- |
| manifest.json | cc56b72e22c4d4544fe102f692532d966c7536b0e1163a4dc20af66705de8e69 |
| cases.jsonl | dcc82bf048cd03b5abf2192a18116a9dcd1b173b9a6467cd1b444d768442db08 |
| observations.jsonl | 562d5c9c2514e6bbf9cb09292c9e4d86780180a468261e731b99e37bd05b394b |
| outcomes.jsonl | 5371e3c99654c5ad11b89fffc5d6174b3bd7e157bfd0029c84a323b7d9791b6d |
| partitions.jsonl | 1577c2eb4ad0f881cd8578d231ae1d618b6f03903f986ed6122f4bc8d6f527cb |
| industry_annotations.json | f0a0ba18f5d2a74511ff10160507c30837eccff956ff7e116ff3603e8cd3bb81 |

