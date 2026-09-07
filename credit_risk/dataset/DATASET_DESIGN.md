# 信用风险新闻数据集 · 设计文档

一个用于评估 **agentic 信用风险分析** 的数据集:每个样本是一家公司
180 天窗口内**逐日到达的新闻标题流**,终点紧邻一个已知结局(破产 /
无事)。模型必须像真实部署一样逐日 ingest、维护记忆、输出风险判决。

- 位置:`data/credit_risk_dataset/`(当前版本 `v0.4/`)
- 规模:204 样本(92 正 / 112 负),17,277 行新闻,事件跨度 2007–2020
- 权威字段说明见 `README.md`;演进纪实见 `DATASET_WORKLOG.md`

---

## 1. 任务定义

```
输入:公司 + 按日期排序的新闻标题流(每天 0~N 条)
输出:每天一个风险判决 {risk_level, score, trend, key_evidence}
评估:窗口内判决 vs 窗口终点后的真实结局
```

为什么这样设计:

- **流式 + 记忆**是被测能力。真实信用监控不是一次性分类,而是状态随
  新闻逐日演化。样本必须支持多日回放,因此保留全部中间日而不只是
  "窗口末一次性判断"
- **只用标题**。源数据(FNSPID)正文基本为空,这是约束也是现实:
  标题级信号足够弱,才考得出记忆聚合和去噪能力
- **防前视**。正例窗口 = `[event_date - 180d, event_date - 1d]`,
  事件当天新闻整体排除(当天报道会直接说"已申请破产")。全库 0 泄
  漏违规,每版重建时都复查

## 2. 数据来源与构建管线

| 环节 | 来源 / 方法 |
|---|---|
| 破产事件(正例标签) | SEC EDGAR 全文检索:8-K 中匹配 `Item 1.03`(Bankruptcy or Receivership),每公司(CIK)取首次,2004–2023 → 1,806 候选 |
| 事件核实 | 抓每个候选的 8-K **正文**,要求 `Item 1.03` 是真实章节标题且含破产关键词(chapter 7/11、petition、receivership)。杀掉 FTS 假阳性(如 AT&T 信贷 8-K,附件违约条款里引用了 "Item 1.03")。子公司共同申报人工复核,唯一例外 LendingTree 用 `event_overrides.csv` 覆盖 |
| 新闻 | FNSPID `All_external.csv`(Benzinga 等聚合,1999–2023,5.7GB),字段:标题/URL/发布者/日期 |
| 公司↔新闻关联 | 规范化公司名的整词匹配(标题+URL)。**不能用 ticker**:破产公司退市后 EDGAR 不保留 ticker |
| 匹配质量门 | 候选须过至少一条证据规则:**R1** 某 symbol 覆盖 ≥30% 匹配行;**R2** 多词公司名作为连续短语出现在 ≥50% 标题;**R3** 高区分度单词 key(≥7 字符、非常见词)出现率 ≥60%。行级再清洗。人工复核记录 `candidates_review.csv` |
| 负例 | 存活 (symbol, year) 对,新闻密度可比(≥30 条/窗口),2:1 对正例采样,窗口终于采样年 12-20;ETF/基金黑名单(其新闻是行业评论) |

构建脚本(按序):`build_01_events.py`(EDGAR FTS)→
`build_02_match_news.py`(5.7GB CSV 一遍过)→
`build_02b_verify_events.py`(8-K 正文核实)→
`build_03_samples.py`(写 v0.1)→ `build_04_v02.py` →
`build_05_v03.py` → `build_06_v04_risk_labels.py`。

## 3. Schema

```json
{
  "sample_id": "pos_1401106_2011-11-03",
  "type": "positive",
  "company": {"name": "MF Global Holdings Ltd.", "cik": "1401106", "symbol": "..."},
  "window": {"start": "2011-05-07", "end": "2011-11-02"},
  "label": {"event": "bankruptcy", "event_date": "2011-11-03"},
  "split": "train",
  "news_quality": {                    // v0.3 新增
    "rows": 290, "centric_rows": 286, "noise_share": 0.014,
    "by_kind": {"roundup": 4}, "flags": []
  },
  "news": [
    {
      "news_id": "b3f1...",            // sha1(date|title|url) 前16位
      "date": "2011-10-27",
      "title": "Fitch downgrades MF Global; cites risk-taking",
      "url": "...", "publisher": "...",
      "dup_cluster_id": "c187",        // v0.2 新增:近重复簇
      "noise_kind": null,              // v0.3 新增:噪音类别
      "gold_risk_type": "rating_downgrade"   // v0.4 新增:风险类型银标
    }
  ]
}
```

负例:`company.name/cik` 为 null(按 symbol 选取),`label.event` 为
null,含义是"窗口内无破产"(不承诺窗口后不破产)。

**派生日级标签**(不存储,按规则生成):距事件 ≤30 天 critical /
31–90 high / 91–180 medium / 负例 low。同一样本因此同时支持判决级
(最终风险 vs 结局)和轨迹级(升级曲线 vs 倒计时)评估。

## 4. 版本演进

| 版本 | 内容 | 动机 |
|---|---|---|
| v0.1 | 204 样本构建(上表管线) | — |
| v0.2 | + 每行 `dup_cluster_id`(近重复标注);+ 官方 train/dev/test 切分 | 去重是记忆层被测能力,需 ground truth;优化实验需要固定切分 |
| v0.3 | + 每行 `noise_kind`;+ 每样本 `news_quality` 与 flags | pipeline 实测发现 2 个漏报样本是**数据噪音**而非模型问题 → 全库审计 |
| v0.4 | + 每行 `gold_risk_type`(8 类风险类型,LLM 银标) | pipeline 抽取事件有 event_type 分类,但无 ground truth 无法评估 |

每版都是**纯增量标注**:样本、窗口、行序、行数不变,构建时断言,
旧版基线永远可比。

### v0.2 去重标注方法

样本内 union-find:规范化标题完全相同(不限日期,周期性相同标题如月
度公告算重复)或 ±3 天内 token-Jaccard ≥ 0.90;比较前剥通讯前缀
(`UPDATE 2-`、`RPT-`、`CORRECTED-` 等)。结果:17,277 行 →
14,584 簇,重复行 2,693(15.6%)。**重复故意保留在数据里**——源数
据如此,近重复检测正是被测能力。

### v0.2 官方切分

按类别内 `window.end` 时间序 70/15/15(必须按类别:破产集中在危机
年份,全局一刀切 dev 几乎没有正例);公司不跨 split,构建时断言。

| split | 样本 | 正/负 | 新闻行 |
|---|---|---|---|
| train | 142 | 64/78 | 12,253 |
| dev | 31 | 14/17 | 2,696 |
| test | 31 | 14/17 | 2,328 |

### v0.3 噪音审计(4 类 `noise_kind`)

| 类别 | 含义 | 行数 |
|---|---|---|
| `roundup` | 每日涨跌榜电讯,提及公司但无公司级内容 | 729 |
| `foreign_subject` | 标题主角是别家公司(别人 ticker 在、自家不在) | 259 |
| `generic_ctx` | 通用名样本中仅靠行业通用短语匹配上的行 | 50 |
| `person_mention` | 公司 key 是姓氏,命中"CEO Paul McDermott"式高管模式 | ~0(已被 foreign_subject 拦截) |

合计 1,038 行(6.0%);全库噪音率中位数仅 2.8%,噪音高度集中。
样本级 flags:`generic_name` / `noisy`(噪音率>50%)/
`thin_centric`(有效行<15)。**5 个样本被标记**(全部正例):
Global Healthcare REIT(99%)、Bristow(82%)、Emerge Energy(72%)、
McDermott(69%)、Dean Foods(57%)。

### v0.4 风险类型标注

封闭 taxonomy(8 类,与 pipeline 抽取节点共用同一定义):
`debt_default` / `rating_downgrade` / `liquidity_stress` /
`lawsuit_regulatory` / `earnings_warning` / `management_turmoil` /
`operational_shock` / `positive_development`。

方法:deepseek-v4-flash 按 (样本, 日期) 分批打标;`noise_kind` 行
自动置 null 不过 LLM;同 `dup_cluster_id` 只标代表行再传播(省
15.6% 调用);逐样本 checkpoint 可续跑。全程 1.9 小时、$4.6。
结果:4,658 / 17,277 行(27.0%)带非空标签,分布为
positive_development 1,334 / debt_default 913 / liquidity_stress 909 /
lawsuit_regulatory 635 / earnings_warning 394 / operational_shock 206 /
rating_downgrade 150 / management_turmoil 117。
是**银标**(LLM 预标注,按类抽检合格,非人工金标),用于评估
pipeline 风险分类的准确率。

## 5. 示例

### 示例 1:正例 —— MF Global(pos_1401106,train)

标签:2011-11-03 破产(8-K Item 1.03);窗口 2011-05-07 ~ 11-02,
290 行新闻,noise_share 1.4%(干净样本)。gold_risk_type 分布:
liquidity_stress 41 / debt_default 36 / lawsuit_regulatory 32 /
positive_development 20 / rating_downgrade 8 / 其余少量。

新闻流呈现教科书式的风险升级弧线(括号内为 gold_risk_type):

```
2011-05-19  MF Global Posts Loss as Legal, Operational Costs Hurt Profit   (earnings_warning)
2011-09-26  MF Global Fined for Market Manipulation Over Fortis in 2008    (lawsuit_regulatory)
2011-10-16  Regulator directed MF Global to boost its capital: WSJ         (liquidity_stress)
2011-10-25  UPDATE 3-MF Global posts Q2 loss on market volatility          (earnings_warning)
2011-10-26  UPDATE 1-S&P may cut MF Global rating to junk                  (rating_downgrade)
2011-10-27  Some MF Global clients move money away as troubles grow        (liquidity_stress)
2011-10-27  Fitch downgrades MF Global; cites risk-taking                  (rating_downgrade)
2011-10-28  MF Global aims for sale by Monday: source                      (liquidity_stress)
2011-10-31  MF Global Files for Chapter 11                                 (debt_default)
2011-10-31  London Metal Exchange suspends MF Global from trading          (operational_shock)
2011-11-01  CME: MF Global Not In Compliance With Customer Fund Rules      (lawsuit_regulatory)
2011-11-02  CME says MF Global customer shortfall of $633 mln              (liquidity_stress)
```

距事件 90 天起流动性信号出现,31 天起评级下调,事件前 3 天密集爆
发——轨迹级标签(medium→high→critical)与新闻流节奏吻合。
(注:event_date 取 8-K filing 日期 11-03;媒体 10-31 已报道申请破
产,相差数天属已知特性,见"局限"。)

### 示例 2:负例 —— Micron(neg_MU_2018,train)

标签:无事件;窗口 2018-06-23 ~ 12-20,201 行,noise_share 7%。
`company.name` 为 null(负例按 symbol 选取)。正常的公司新闻流:

```
2018-06-26  UBS Upgrades Micron Technology to Neutral
2018-06-26  UBS Upgrades Micron to Neutral from Sell; Raises PT to $60   ← 与上行是近重复(dup_cluster_id 相同)
2018-06-26  DRAMeXchange Says Server DRAM Supply to Improve in Q3
```

也包含干扰项:"Bulls & Bears Of The Week"、"26 Stocks Moving In
Tuesday's Pre-Market Session"(后者是周期性相同标题,跨日期也算重
复簇)——负例同样考核去重与抗噪。

### 示例 3:被标记的噪音样本 —— Global Healthcare REIT(pos_727346,test)

72 行新闻只有 1 行真相关,`news_quality.flags =
["generic_name", "noisy", "thin_centric"]`。公司名里的 "Global
Healthcare" 是行业通用短语,匹配上的全是**别家公司**的大会新闻:

```
2019-05-22  Codexis (CDXS) Presents At UBS Global Healthcare Conference        (noise: generic_ctx)
2019-05-23  Community Health Systems (CYH) Presents At UBS Global Healthcare…  (noise: foreign_subject)
2019-05-23  LHC Group (LHCG) Presents At UBS Global Healthcare Conference      (noise: generic_ctx)
```

这类样本是匹配规则的根本性失败,模型漏报它们不算模型的错——评估时
应报告"含/不含 flagged 样本"两版指标。

## 6. 评估用法

| 维度 | 指标 | 依赖标注 |
|---|---|---|
| 判决 | 正例检出率、负例误报率、预警提前天数 | label |
| 轨迹 | 风险带一致率(严格 / 宽容 ±1 级) | 派生日级标签 |
| 记忆去重 | dedup 精确率/召回率 | v0.2 `dup_cluster_id` |
| 风险分类 | event_type 准确率/逐类召回 | v0.4 `gold_risk_type` |
| 数据质量分层 | 含/不含 flagged 样本两版报告 | v0.3 `news_quality` |

参考基线(本仓库 pipeline,test 集):正例检出 12/14(剔除 flagged
后 12/12),平均提前 96 天,误报 1/17,dedup P=0.53/R=0.97,风险带
宽容一致率 0.95。

## 7. 已知局限

- 只有标题,无正文;8-K 全文不在样本内(v0.4+ 候选)
- 源数据 15.6% 重复行,故意保留(已标注)
- 负例无公司名,标签只断言"窗口内未破产"
- 个别银行控股/金融公司(Colonial、UCBH、MF Global)的 event_date
  取 8-K filing 日期,媒体可能提前 1–3 天报道,标签日期可差数天
- `company.symbol` 是匹配证据推断值,可能被污染(如 MF Global 的
  symbol 显示为 IBKR——收购谈判方的新闻占比过高所致),仅作参考
- 正负比 ~1:1.2 是人为设定,不代表真实破产频率
- 名字匹配噪音:v0.3 已标注 6.0% 行、5 个样本;启发式标注与 LLM 银
  标均有小概率双向误差
- EDGAR Item 1.03 在 2004 年前覆盖稀薄

## 8. 复现

```bash
.venv/bin/python data/credit_risk_dataset/build_01_events.py          # ~10 min, EDGAR FTS
.venv/bin/python data/credit_risk_dataset/build_02_match_news.py      # 5.7GB CSV 一遍过
.venv/bin/python data/credit_risk_dataset/build_02b_verify_events.py  # 8-K 正文核实
.venv/bin/python data/credit_risk_dataset/build_03_samples.py         # -> v0.1/
.venv/bin/python data/credit_risk_dataset/build_04_v02.py             # -> v0.2/(去重标注+切分)
.venv/bin/python data/credit_risk_dataset/build_05_v03.py             # -> v0.3/(噪音标注)
.venv/bin/python data/credit_risk_dataset/build_06_v04_risk_labels.py # -> v0.4/(风险类型银标,需 DEEPSEEK_API_KEY)
```
