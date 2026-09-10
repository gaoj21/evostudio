# Credit-Risk 数据集 — 交接文档

> 当前完整构建方法、统计口径及公司/轨迹目录见 [数据集构建与数据说明](dataset/DATASET_CONSTRUCTION_AND_PROFILE.md)。

> 当前 100 家公司清洗版本：`dataset/expansion/releases/2026-09-09-industry-v1/`。
> 评测准备状态见 [最新清洗审计](../dataset/review/READINESS_2026-09-09.md)。尚未具备完整 gold 标签。
> 下文的 41 家统计属于早期版本，不能作为当前版本验收依据。
> 本文只描述最终状态;过程纪实在 `DATASET_WORKLOG.md`,设计推演在
> `DATASET_DESIGN.md` / `DATASET_DESIGN_EN.md`。

## 1. 两个数据集的关系

| | **v0.4(历史主数据集)** | **contemporary(当代数据集)** |
|---|---|---|
| 时间覆盖 | 2016~2023 | **2025-01 ~ 2026-08** |
| 新闻源 | FNSPID(标题) | GDELT(标题)+ 出版方爬取(**全文**) |
| 监管文件 | 无 | **EDGAR 8-K 全文**(667 份) |
| 规模 | 204 样本(92 正/112 负),17277 行 | 41 公司(26 正/15 负),4926 行新闻 |
| 用途 | 已完成基线评估 + MIPRO 优化 | agentic pipeline 的双通道评估 |

v0.x 的正文补不上(旧链接 403/404、Wayback 无存档),这是构建
contemporary 的核心动机:链接新鲜可爬,且增加 8-K 这一"确定事实"通道。

## 2. contemporary 数据集最终构成

### 2.1 公司池(41 家)

- **正例 26 家**:2025-01~2026-08 在 EDGAR 提交 Item 1.03(破产)8-K 的公司。
  EDGAR 全文检索得 83 个唯一公司,逐家核对 8-K 正文确认 REAL;再经新闻
  密度门(见 3.2)淘汰小盘低覆盖公司,剩 26 家。知名案例:Nikola、
  23andMe、Canoo、Wolfspeed、Sunnova、WeightWatchers、Spirit Airlines、
  iRobot、Luminar、Sonder、Cumulus Media、Sleep Number、EchoStar 等。
- **负例 15 家**:人工挑选的同期存活公司,健康与承压混合(UAL、Frontier、
  Macy's、Rivian、Lucid、Wayfair、Moderna、Pfizer、BioNTech、MARA、
  Wendy's、Dine Brands、Beyond Meat 等)。ref_date 从正例事件日期随机
  采样(seed 7),对齐宏观环境。
- 另有 13 家因 GDELT 持续不可用/限流放弃(Tesla、AMC、GameStop、JetBlue、
  Airbnb、Hilton 等),已打标记,如未来补抓只需删掉对应 `.dropped` 文件。
- 另有 2 家正例因 query_name 缺陷清除:CHARLES & COLVARD 的查询词被截断成
  "CHARLES &",捞回 189 条全是同名噪声("King Charles" 级);Broad Street
  Realty 的 "Broad Street" 太泛。修正查询词后重抓,真实覆盖只有 3/0 条,
  确认淘汰——这也顺带验证了评估 pipeline 的 grounding 对噪声的拦截能力。

### 2.2 每个公司的三个数据源

| 通道 | 内容 | 规模 |
|---|---|---|
| 新闻标题 | GDELT artlist,事件日前 **180 天**窗口,`{date, title, url, publisher}` | 共 4926 行 |
| 新闻全文 | 按 URL 直接爬出版方,trafilatura 抽正文(≤15000 字符) | 单条成功率 50~65%(404/付费墙属正常损耗) |
| 8-K 全文 | 窗口内全部 8-K,正文 ≤30000 字符 | 667 份;Item 分布:9.01×458 / 7.01×172 / 5.02×170 / 1.01×159 / 2.02×105 / 2.03×79 / 3.01×61 |

### 2.3 已知噪声

- `pos_NKLA`:约两成是篮球运动员 Nikola Jokić 的新闻(单词名碰撞,
  标题门挡不住);少数非英语条目(GDELT 语言标注不准)。使用时注意。
- 同一件事被多家小站转载(辛迪加内容),全文可能雷同;标题级已按
  (date, 规范化标题)去重,但跨日期的转载仍可能存在。

## 3. 目录与 Schema

```
contemporary/
├── events_recent.csv      # R01:破产事件。cik, company_name, event_date,
│                          #      form, adsh, verdict(REAL), subsidiary_mention
├── candidates.csv         # R02:公司池。type(positive|negative), cik, name,
│                          #      query_name(查询用短名), symbol, ref_date
└── raw/
    ├── company_tickers.json        # SEC 官方 CIK↔ticker 表(10388 条)
    ├── news/<pos|neg>_<SYM>.jsonl  # R02:{date, title, url, publisher}/行
    │   ├── *.jsonl.meta.json       #      {window: {start,end}, n_requests}
    │   └── *.jsonl.dropped         #      淘汰标记(空文件;存在即跳过)
    ├── fulltext/<pos|neg>_<SYM>.jsonl  # R05:新闻行 + text, fetch_status,
    │                                   #      n_chars
    └── filings/<pos|neg>_<key>.jsonl   # R03:{filing_date, form, adsh,
                                        #      items[], text}/行
```

`fetch_status` 取值:`ok`(≥200 字符)/ `thin` / `http403` 等 / `empty` /
`error:<异常类名>`。

## 4. 构建链(全部从仓库根目录运行)

| 步骤 | 脚本 | 干什么 |
|---|---|---|
| R01 | `build_r01_recent_events.py` | EDGAR 全文检索 Item 1.03,核对正文,出 `events_recent.csv` |
| R02a | `build_r02_candidates.py` | 事件→正例 + 手挑负例,出 `candidates.csv` |
| R02b | `build_r02_fetch_news.py` | GDELT 抓新闻标题(详见第 5 节) |
| R03 | `build_r03_fetch_8k.py` | EDGAR 抓窗口内 8-K 全文(带断点) |
| R04 | `build_r04_assemble.py` | 组装 `samples/train/dev/test.jsonl + manifest.json` |
| R05 | `build_r05_fetch_fulltext.py` | 按 URL 爬新闻全文(8 线程,带断点) |

所有脚本都有断点:重跑同一条命令自动跳过已完成部分。

## 5. GDELT 抓取的关键参数与教训(R02b)

参数:`WINDOW_DAYS=180`、`MIN_NEWS=20`(窗口内新闻行数门限)、
`MAXRECORDS=250`、`PAUSE=8s`、`WORKERS=3`、`TIMEOUT=150s`、
每公司请求预算 `MAX_REQUESTS=60`、二分封底 7 天。

标题门(R2 门):标题必须含查询名的全部关键词(去公司后缀、≥3 字符)。

**教训(重跑/换源前必读)**:

1. **宽窗口静默截断**:一次查 180 天,GDELT 高负载时会返回 200 但只有
   部分数据(实测同一公司 19 条→12 条)。必须按 ≤60 天块查询。
2. **并发 >3 触发 429**;429 用 30~300s 阶梯退避。
3. **服务器白天(中国时间)经常整体无响应**,夜间快好几倍;大批量抓取
   安排过夜。
4. 新闻爆炸的公司(Nikola 196 条用 62 个请求)必须靠"7 天封底 + 请求
   预算"兜住,否则递归二分会烧几百个请求。
5. ERROR 的公司**不留标记**,重跑自动补;淘汰的公司留 `.dropped` 标记,
   重跑跳过。

## 6. v0.4 主数据集(历史,已收官)

`v0.4/`:204 样本(92 正/112 负)、17277 行、train/dev/test = 142/31/31。
- `dup_cluster_id`(v0.2):同日改写重报聚类
- `noise_kind` + `news_quality`(v0.3):5 个 flagged 样本
- `gold_risk_type`(v0.4):LLM 银标,8 类,覆盖率 27.0%(4658 行非空);
  分布:positive_development 1334 / debt_default 913 / liquidity_stress 909 /
  lawsuit_regulatory 635 / earnings_warning 394 / operational_shock 206 /
  rating_downgrade 150 / management_turmoil 117

基线成绩(test,旧 3 节点 pipeline):检出 12/12(剔除 flagged)、平均提前
96 天、误报 1/17、dedup P=0.53/R=0.97、宽容一致率 0.95。MIPRO 优化结论:
种子指令即最优,见 `projects/credit_risk/output/mipro/best_program.json`。

## 6b. 切分变更(2026-09-08,R07)

只保留 **dev / test** 两个切分,去掉 train:按类内 `window.end` 时间序,
最早 60% 进 dev(prompt 调优、Evolve 学习都在这里,优化器内部自己再分
教/判),其余 test(只用于最终数字)。正例 16/10,负例 9/6,dev 25、
test 16。`build_r07_resplit.py` 原地改写 `split` 字段,样本内容、
news_quality 不变;`build_r04_assemble.py` 的规则已同步,重建也得到同样切分。

## 7. 组装产物(R04,已生成)

`contemporary/` 下:`samples.jsonl`(41)+ `train.jsonl`(28)/ `dev.jsonl`(6)
/ `test.jsonl`(7)+ `manifest.json`。

- 切分:按类内窗口结束日 chronologically 70/15/15，一公司只进一个集合。
  正例 18/4/4,负例 10/2/3。
- 样本 schema(v0.4 schema + filings 通道):`sample_id, type, company,
  window, label, split, news[], filings[]`;news 行带 `text` 的 2751/4926(56%)。
- 3 个样本窗口内无 8-K(正常,不是所有公司窗口内都有申报)。

## 8. 后续工作

- ~~`run_contemporary_eval.py`~~ 已完成,结果见
  `projects/credit_risk/output/eval_contemporary/REPORT.md`(7/8 检出、平均提前
  ~93 天、误报 1/6)
- NKLA 的 Jokić 噪声清洗(如需)
- 若扩容:13 家放弃的公司删 `.dropped` 即可补抓;或注册 Finnhub 做主源
