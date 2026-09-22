# 信用风险新闻数据集工作总结

> 记录 `data/credit_risk_dataset/` 从 v0.1 到 v0.3 的完整演进:构建方法、
> 每版新增内容、评测中发现的问题及其修复。数据集的权威说明仍以
> `README.md` 为准,本文档是面向人的工作纪实。

## 0. 数据集是干什么的

评估"agentic 信用风险分析"流水线:每个样本是**一家公司 + 一个 180 天
窗口内逐日到达的新闻标题流**,窗口终点紧邻一个已知结局(破产申请,或
无事发生)。流水线需要像真实场景一样逐日 ingest 新闻、维护记忆、每天
给出风险判决。它同时支持两种评估:

- **判决级**:窗口内最终/最高风险等级 vs 真实结局(检出率、误报率)
- **轨迹级**:风险升级曲线 vs 距事件天数(≤30 天 critical / 31–90 high /
  91–180 medium / 负例 low,该标签是按规则派生的,不存储)

## 1. v0.1:从零构建

构建管线(5 个脚本,按序运行,见 README):

| 环节 | 做法 |
|---|---|
| 破产事件(正例标签) | SEC EDGAR 全文检索 8-K 中匹配 `Item 1.03`(Bankruptcy or Receivership)的 filing,每个 CIK 取首次,2004–2023,得 1,806 个候选事件 |
| 事件核实 | 抓取每个候选 8-K 的正文(SGML 中第一个 `<TYPE>8-K` 文档),要求 `Item 1.03` 是真实章节标题 **且** 出现破产关键词(chapter 7/11、voluntary petition、receivership 等)。这一步杀掉了 FTS 假阳性(如 AT&T 的信贷额度 8-K,其附件在违约条款里引用了"Item 1.03")。子公司共同申报被归类为 co-debtor(真实)或 review,review 桶全部人工审计过,唯一仅子公司申报的 LendingTree 用 `event_overrides.csv` 人工覆盖 |
| 新闻 | FNSPID `Stock_news/All_external.csv`(Benzinga 等,1999–2023,约 5.7GB)。只有标题/URL/发布者/日期——正文列基本为空 |
| 公司↔新闻关联 | 规范化公司名的整词匹配,对标题+URL。破产公司退市后 EDGAR 不保留 ticker,所以**无法用 ticker 映射**,只能按名字匹配——这是后续一切匹配噪音的根源 |
| 匹配质量门 | 候选公司须满足至少一条证据规则:**R1** 某个 `Stock_symbol` 覆盖 ≥30% 匹配行;**R2** 多词公司名作为连续短语出现在 ≥50% 匹配标题中;**R3** 有区分度的单词 key(≥7 字符、非常见英文词)出现率 ≥60%。行级再清洗(确认的 symbol 或短语在标题中才保留)。人工复核记录于 `candidates_review.csv` |
| 负例 | 存活的 (symbol, year) 对,新闻密度可比(≥30 条),按 2:1 对正例采样,窗口终于采样年的 12 月 20 日;ETF/基金进黑名单(其新闻是行业评论而非公司新闻) |

**防泄漏**(`build_03_samples.py`):正例窗口 = `[event_date - 180d,
event_date - 1d]`,事件**当天**的新闻被排除(当天报道会提到破产申请本
身)。v0.1 全库验证 0 泄漏违规,v0.2 输出上又复查过一次。

**v0.1 规模**:92 正 + 112 负 = 204 样本,17,277 行新闻,事件日期跨度
2007-10-17 ~ 2020-07-31。雷曼、WaMu、GM、Tribune、Six Flags、Kodak、
Borders、Sears、Hertz、PG&E、Chesapeake、Whiting 等知名案例都在。

## 2. v0.2:重复标注 + 官方切分

`build_04_v02.py`,纯后处理(样本、窗口不变),两个新增:

**1) 每行新闻加 `dup_cluster_id`**(近重复聚类,用于评估记忆层的去重
行为)。方法:每个样本内做 union-find,合并规则是规范化标题完全相同
(不限日期——月度 `REG-...` 这类周期性相同标题也算重复)**或** ±3 天
内 token 集合 Jaccard ≥ 0.90;比较前剥掉通讯社前缀(`UPDATE 2-`、
`RPT-`、`WRAPUP 4-`、`CORRECTED-` 等)。簇 id 样本内编号(`c000`,
`c001`,…),按簇内最早日期排序,单节点自成一簇。

结果:17,277 行 → 14,584 簇,重复行 2,693(15.6%)。只有 41 个簇包含
不完全相同的标题,人工抽查确认都是真重复(播客标题词序变体、来源后
缀变体)。重复被**故意保留**在数据里——近重复检测本身就是记忆层被考
核的能力,标注只是为了能打分。

**2) 官方 train/dev/test 切分**。按类别(正/负)内分别按 `window.end`
时间序 70/15/15 切——必须按类别切,因为破产集中在危机年份,全局一刀切
会让 dev 几乎没有正例。同一公司(正例按 CIK、负例按 symbol)不跨
split,构建时断言。

| split | 样本 | 正/负 | 新闻行 | window.end 范围 |
|---|---|---|---|---|
| train | 142 | 64/78 | 12,253 | 2007-10-16 .. 2018-12-20 |
| dev   | 31  | 14/17 | 2,696  | 2016-09-27 .. 2019-05-12 |
| test  | 31  | 14/17 | 2,328  | 2018-12-20 .. 2020-07-30 |

## 3. 用数据集实测 pipeline(v0.2,重要发现)

用 `examples/projects/credit_risk/run_dataset_eval.py` 在 dev/test 上跑了全量基线
(逐样本 checkpoint 续跑、6 切片并行;花费 dev $2.24 / test $6.23)。
test 集结果:

- 正例检出 **12/14**;平均预警提前 **96 天**(中位 92 天)
- 负例误报 **1/17**(neg_WRLD,次级贷款公司,新闻面确实差)
- 去重质量:P=0.53 / R=0.97
- 风险带一致率:严格 0.48,宽容 ±1 级 0.95(系统性"报早"是设计取向)

**关键:2 个漏报正例不是模型问题,是数据问题**——这两个样本的新闻流
本身就是噪音:

- `pos_73887`(Bristow Group,2019-05 破产):74 行新闻里 61 行是
  "X and Y among Energy/Materials gainers/losers" 每日涨跌榜电讯,公司
  名确实出现了,但没有任何公司级信息量
- `pos_727346`(Global Healthcare REIT,2019-10 破产):公司名里的
  "Global Healthcare" 是行业通用短语,匹配上的全是**别家公司**的
  "UBS/Jefferies/Goldman Sachs Global Healthcare Conference" 参会幻灯片
  新闻,真公司新闻约等于零

这次评测还顺带修了 pipeline 自身的三个缺陷(dedup 批内去重 + 数字守
卫、无事件日沿用 profile 而非硬性归零、archive 重复添加),修复后才有
上面的基线数字。MIPRO 提示词优化(v0.2 train 子集,15 个样本内选出)
最终确认现有提示词已是局部最优(种子 90.93/94.69/96.46 vs 最优候选
96.44,差异在 6 样本验证集的天然波动 ±3~5 分之内),未改动数据集。

## 4. v0.3:全库噪音审计 + 行级标注

发现两个问题样本后,问题变成:**全库还有多少这样的样本?** 于是写了
`build_05_v03.py`,对 204 个样本 17,277 行逐行启发式分类。**只标注、
不删除**——样本、窗口、split、行序、行数与 v0.2 完全一致(构建时断
言,v0.2 的所有基线保持可比)。

**每行加 `noise_kind`**(null = 公司相关行):

| 类别 | 含义 | 行数 |
|---|---|---|
| `roundup` | 每日涨跌榜/异动榜电讯,公司被提及但无公司级内容 | 729 |
| `foreign_subject` | 标题主角是别家公司(有别人的 `(TICKER)`,没有自家的,公司名不在句首) | 259 |
| `generic_ctx` | `generic_name` 样本中仅靠通用短语匹配、无 ticker 证据的行 | 50 |
| `person_mention` | 公司 key 是姓氏,命中高管头衔模式("CEO Paul McDermott") | 0(真实命中已被 foreign_subject 提前拦截) |

合计 1,038 行噪音(6.0%)。全库噪音率中位数仅 **2.8%**——绝大多数样
本是干净的,噪音高度集中。

**每个样本加 `news_quality`**:`{rows, centric_rows, noise_share,
by_kind, flags}`,flags 规则:`generic_name`(公司名所有 key token 都
是行业/地域通用词,名字匹配不可靠)、`noisy`(噪音率 >50%)、
`thin_centric`(窗口内公司相关行 <15)。

**5 个样本被标记**(全是正例):

| 噪音率 | 样本 | split | 病因 |
|---|---|---|---|
| 99% | pos_727346 Global Healthcare REIT | test | generic_name + noisy + thin_centric |
| 82% | pos_73887 Bristow Group | dev | noisy + thin_centric(榜单刷屏) |
| 72% | pos_1555177 Emerge Energy | test | noisy + thin_centric(榜单刷屏) |
| 69% | pos_708819 McDermott International | test | noisy(人名撞车 + 榜单) |
| 57% | pos_931336 Dean Foods | test | noisy(榜单量大,但仍有 59 条真新闻,可用) |

评测时发现的两个坏样本全部被自动抓获,另新挖出 3 个同类。

**构建过程中修掉的三个启发式 bug**(都曾造成误标,全部复盘验证过):

1. key token 提取最初丢弃 <4 字符的 token,把 "MF"/"RCS"/"XXI" 这类短
   但高区分度的缩写扔了,导致 MF Global、A123 Systems、Energy XXI、
   RCS Capital 4 个样本被错标为 100% 噪音 → 改为保留含数字或原名中全
   大写的短 token
2. 公司名字段里的括号 ticker 注记("...INC. (GBCS)")污染 key token,
   使 Global Healthcare REIT 漏标 → 提取前先剥括号
3. 人名正则整体大小写不敏感,"at/of/Says/Applauds" 被当成名字中间的
   词,把 Lehman、Dynegy、Tribune 等 10 条真新闻误标 → 人名部分恢复
   大小写敏感(仅 key 用 `(?i:...)` 局部忽略),加停用词守卫

**用法**(README 有同样说明):头条指标报告"含/不含 flagged 样本"两
版(如 test 基线:原始 12/14,剔除已标记噪音样本后 12/12);行级过滤
直接丢弃 `noise_kind != null` 的行,过滤后所有未标记样本仍 ≥24 条有
效新闻。

## 5. v0.4:风险类型 gold 标注(银标)

pipeline 的 extract 节点按 8 类封闭 taxonomy 给事件分 event_type,但数据
集没有对应 ground truth,风险分类质量无法打分。`build_06_v04_risk_labels.py`
用 deepseek-v4-flash 给每行新闻加 `gold_risk_type`(8 类之一或 null):

- 按 (样本, 日期) 分批打标,taxonomy 全文进 prompt;`noise_kind` 行自动置
  null 不过 LLM;同 `dup_cluster_id` 只标代表行再传播(省 15.6% 调用);
  逐样本 checkpoint(`v0.4/labels_ckpt/`),断网重跑自动续
- 全程 1.9 小时、$4.6、790 万 token,0 报错

结果:17,277 行中 4,658 行(27.0%)带非空标签。分布:
positive_development 1,334 / debt_default 913 / liquidity_stress 909 /
lawsuit_regulatory 635 / earnings_warning 394 / operational_shock 206 /
rating_downgrade 150 / management_turmoil 117。

按类随机抽检(每类 5 条):绝大多数正确——破产申请、评级下调、契约违
规警告、高管离职、停产/砍合同都判得准。发现的个别误差:临床试验暂停
被判成 management_turmoil(应为 operational_shock);分析师下调偶尔进
了 rating_downgrade(taxonomy 本意是评级机构)。定性为**银标**:适合
算逐类召回/准确率指标,不适合逐行审计。

自检样本(KaloBios)还验证了重复簇传播:同一标题 4 行只标 1 次、标签
一致;`noise_kind` 行全部自动 null。

## 6. 四版演进一览

| | v0.1 | v0.2 | v0.3 | v0.4 |
|---|---|---|---|---|
| 样本数 | 204(92正/112负) | 同 v0.1 | 同 v0.2 | 同 v0.3 |
| 新闻行 | 17,277 | 同(+`dup_cluster_id`) | 同(+`noise_kind`/`news_quality`) | 同(+`gold_risk_type`) |
| 新增 | 事件+新闻+匹配+负例 | 重复簇标注、官方切分 | 噪音标注、5 个 flagged 样本 | 8 类风险类型银标 |
| 脚本 | build_01/02/02b/03 | build_04_v02.py | build_05_v03.py | build_06_v04_risk_labels.py |
| 兼容性 | — | 与 v0.1 同行同序 | 与 v0.2 同行同序(断言) | 与 v0.3 同行同序(断言) |

## 7. 已知局限(沿用 README,评测中实证的加了注)

- **只有标题**:FNSPID 正文基本为空,样本只含标题;8-K 正文不在数据
  集内(v0.5 候选)
- **源数据重复**:约 15.6% 重复行,故意保留,v0.2 已标注
- **负例无公司身份**:按 ticker 选取,`company.name` 为 null;标签只
  断言"窗口内未破产",不排除窗口后破产
- **银行控股类正例**:个别区域银行(Colonial、UCBH)披露事件是子公司
  被 FDIC 接管,控股公司数日后才申请 Chapter 11,event_date 可能差几天
- **类别先验是人为的**:约 1:1.2 的正负比不代表真实破产频率(目标
  1:2,部分负例窗口达不到 30 条新闻密度而落选)
- **名字匹配噪音**(v0.3 已标注):通用行业短语公司名、姓氏公司名、
  榜单电讯刷屏,合计 6.0% 行、5 个样本被标记;启发式标注本身有少量
  双向误差,抽查干净
- EDGAR Item 1.03 在 2004 年前覆盖稀薄

## 8. v0.5 候选方向

- 扩大正例:`MIN_NEWS` 放宽到 20,或窗口扩到 365 天
- 可选:SEC 8-K 正文作为额外信号源
- 负例补齐公司身份(symbol → 公司名解析),让所有样本都有
  `company.name`
