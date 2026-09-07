# Agentic Credit-Risk Alert Pipeline — 交接文档

> 最终版,对应代码 `credit_risk/agentic_pipeline/`(934 行,8 个模块)。
> 框架层依赖:`evoagentx/`(LTM、Skill、HITL、Message)。
> 应用层共享件:`credit_risk/credit_risk_demo.py`(记忆助手函数)。

## 1. 设计目标

旧版(`credit_risk_demo.py`,3 节点顺序流)是"回放已知公司的新闻窗口":
公司是谁、要不要管,都是输入里写死的。

这一代解决的是**真实监控场景**:原始新闻/公告不带归属地涌进来,pipeline
自己要回答三个问题——**这条消息是关于谁的?这家公司我们管不管?值不值得
报警?**

## 2. 总体架构(8 层)

```
                   ┌──────────────── 1. SOURCING ────────────────┐
  原始新闻标题 ────┤ LLM 实体抽取:标题的【主语】是谁               │
  原始 8-K 公告 ───┤ 直接从 EDGAR 元数据读 registrant CIK(不用 LLM)│
                   └──────────────┬───────────────────────────────┘
                                  ▼ CandidateItem(name | CIK)
                   ┌────────────── 2. GROUNDING ─────────────────┐
                   │ ObligorRegistry:CIK 直查 → 规范化名称 → 别名 → │
                   │ token 模糊。匹配不上 → 丢弃并记日志            │
                   └──────────────┬───────────────────────────────┘
                                  ▼ 按 (obligor, date) 分组
        ┌──────────────────── 3. REASONING LOOP ─────────────────────┐
        │ DETECT      主语确认 + topic 分类(8 类闭集)+ 严重度/置信度   │
        │ INVESTIGATE 纯检索(不调 LLM):风险档案 + 历史告警 + 相似案例  │
        │ REFLECT     结合档案和历史校准 topic/severity/confidence     │
        │ DECIDE      按评分 rubric 出判决:alert | suppress           │
        └──────────────────────┬──────────────────────────────────────┘
                               ▼
                   4. ACTION:alert → 告警记录 / suppress → 记理由
                               ▼
                   5. MEMORY:告警库(LTM)+ 风险档案(focus+trajectory)
                            + 新闻归档(供去重)
                               ▼
                   7. EVALUATION:Weight of Evidence 打分;
                      灰区 (0.35, 0.65) 路由人工反馈(AutoApprover / HITL)
                               ▼
                   8. SELF-EVOLVING:prompt 是模块常量,可接 MIPRO;
                      export_traces 导出 agent IO 供离线打分;
                      拓扑搜索(loop 是普通函数链,改起来便宜)
```

注意:没有第 6 层——用户原始设计里 6 是编号跳过的,实现保持了这个编号。

## 3. 模块逐个说

### 3.1 `sourcing.py`(108 行)— 第 1 层

双通道输入,统一输出 `CandidateItem`。

核心数据结构:

```python
@dataclass
class RawItem:
    source: str            # "news" | "8k"
    date: str              # YYYY-MM-DD
    title: str
    text: Optional[str]    # 8-K 正文走这里;新闻只有标题时为 None
    url, publisher, cik, registrant_name, items, meta
```

- **新闻通道**:`extract_entities()` 批量(每次 ≤30 条,`MAX_ITEMS_PER_CALL`)
  问 LLM "每条标题的主语公司是谁"。关键设计:只要**主语**,顺带提到的公司
  (对手方、同行列表、会议地点)不算——这一步就把综述类电讯(roundup wire)
  挡在门外,这是 v0.3 数据集里发现的主要噪声源。解析失败返回空列表而不是异常,
  该条目随后在 grounding 被丢弃。
- **8-K 通道**:`candidates_from_filings()` 纯解析,CIK 直接从 EDGAR 元数据读,
  不调 LLM。

### 3.2 `obligors.py`(123 行)— 第 2 层

`ObligorRegistry` = 内部债务人名单 + 匹配器。三级匹配:

1. `match_cik()`——CIK 直查(去前导零),最强证据
2. `match_name()` 精确——名称规范化(小写、去标点、去公司后缀如
   Inc/Corp/Group/LLC 等 24 个)后查索引,别名(alias)同索引
3. `match_name()` 模糊——token 集合双向包含,且较短一侧 ≥2 个 token 或
   含 ≥5 字符的区分性词(防 "Delta" 这种单词歧义)

`from_candidates_csv()` 直接从数据集的公司清单加载名单(模拟生产环境的
内部债务人主数据),`query_name`(新闻里的常用短名)自动注册为别名。

**坑**:`Obligor` 必须 `@dataclass(eq=False)`,否则作 dict key 时 unhashable。

### 3.3 `agents.py`(176 行)— 第 3 层,四个 agent

| Agent | LLM? | 注入 Skill | 输入 | 输出 JSON |
|---|---|---|---|---|
| `detect` | ✅ | **credit_risk_taxonomy** | 当日该 obligor 的条目批次 | `is_main_subject, topic, severity, confidence, summary` |
| `investigate` | ❌ 纯检索 | — | obligor + 查询文本 | `{profile, profile_id, past_alerts, cases_text}` |
| `reflect` | ✅ | — | detection + investigate 的上下文 | `{topic, severity, confidence, adjusted, rationale}` |
| `decide` | ✅ | **risk_scoring_rubric** | validated detection + reflection + context | `{action: alert\|suppress, risk_level, score, rationale}` |

- severity 五档:`critical/high/medium/low/positive`
- reflect 的定位是**校准器**:已报警事实的重复报道要衰减;存在未解除的
  confirmed 事件时,升级必须维持(不允许新的小新闻把分拉低)
- reflect 的返回值只覆盖 topic/severity/confidence 三个键
  (`pipeline.py` 里的 `validated = {**detection, **reflection调整项}`)
- 所有 prompt 都是模块级常量(`DETECT_PROMPT/REFLECT_PROMPT/DECIDE_PROMPT`
  + sourcing 的 `ENTITY_EXTRACTION_PROMPT`),这是故意的:自进化层要优化它们

### 3.4 `pipeline.py`(252 行)— 编排(第 3~5 层)+ 装配

`AlertPipeline.ingest(items) -> PipelineResult` 是主入口:

1. 按 source 分流,news 走实体抽取、8k 走 CIK
2. `_ground()`:候选过 registry,按 `(obligor, date)` 分组
3. `_process_group()` 逐组执行:
   - **去重先行**:`dedup_news` 对照新闻归档,全是重复的整组跳过
     (同一天的改写重报只会产生一条告警)
   - detect → 不是主语/无事件 → suppress 返回
   - investigate → reflect → decide
   - **action**:alert → 先进 WoE 评估和人工反馈钩子,再写入告警库;
     suppress → 记理由
   - **记忆更新**:`archive_news` 归档新闻;`update_profile` 用当次事件
     和判决更新风险档案(事件列表 + risk_level/score/trend)

`AlertRepository`:告警记录存 LTM,`record_type="alert"`,
`wf_goal=f"credit_risk:{obligor_id}"` 做命名空间;`alerts_for()` 用
相似度检索再按 obligor_id 精确过滤、按日期排序取最近 10 条。

`build_pipeline(llm, registry, store_dir=None, fresh=True)` 是装配函数:
从 `credit_risk_demo` 注入 7 个记忆助手(`memory_fns` dict,**注入而非
复制**——改 demo 里的实现,两处同时生效),加载两个 skill 的文本注入
detect/decide。`store_dir` 可覆盖 demo.STORE_DIR(记忆落盘位置);
`fresh=True` 重建 LTM。

`PipelineResult` 四个列表:`alerts / suppressed / dropped_unmatched / log`。

### 3.5 `evaluate.py`(97 行)— 第 7 层

WoE 公式(0~1,越高越可信):

```
0.30×severity + 0.25×confidence + 0.15×来源(8k=1.0/news=0.8)
+ 0.15×corroboration(同批独立条目数:0.5+0.25×(n-1),封顶 1.0)
+ 0.15×轨迹一致性(延续/升级既有风险=0.9,逆历史降级=0.6,无档案=0.7)
```

- 灰区 `(0.35, 0.65)` 路由人工;区外自动通过
- `AutoApprover`:默认钩子,只记录路由、不做决定(演示/回放用)
- `make_hitl_hook(hitl_manager)`:接 `evoagentx.hitl.HITLManager` 的适配器,
  APPROVE_REJECT + POST_EXECUTION + 300s 超时,返回
  `{routed_to, approved, feedback, woe}`

### 3.6 `evolve.py`(43 行)— 第 8 层

`export_traces(result, out_path)`:把一次运行的 PipelineResult 导出成
JSONL,每条含 detect/reflect/decide 的完整输入输出——候选 prompt 可以
**离线打分**,不用重跑 pipeline。

优化路径:
- prompt 优化:把 pipeline 包成 MiproRegistry program 喂给
  `MiproOptimizer`,方法照抄 `credit_risk/optimize_mipro.py`(旧版 3 节点
  的 MIPRO 已收官,结论是种子指令即最优)
- 拓扑搜索:loop 就是 `_process_group` 里的普通函数链,去掉 Reflect、
  调换 Investigate/Detect 顺序等变体都很容易写,可接 AFlow

### 3.7 `run_demo.py`(122 行)— 端到端冒烟

合成数据源验证五个关键行为,全部通过:

1. 未知公司 → grounding 丢弃
2. 综述电讯提到被监控公司 → 实体抽取判"非主语"→ 丢弃
3. 同日改写重报 → 去重拦截,只出一条告警
4. 8-K(CIK 通道)→ 对照前日告警升级(85→95)
5. 灰区 WoE(0.52)→ 触发人工路由钩子

运行:`.venv/bin/python credit_risk/agentic_pipeline/run_demo.py`(约 $0.01)

## 4. 记忆系统(继承自 credit_risk_demo)

`memory_fns` 注入的 7 个函数:

| 函数 | 用途 |
|---|---|
| `dedup_news` | 对照归档判重(改写重报拦截) |
| `archive_news` | 新闻写入归档 |
| `recall_profile` | 取公司风险档案 → (profile, profile_id) |
| `save_profile` | 写回档案(LTM add/update) |
| `update_profile` | LLM 融合当次事件进档案(focus + trajectory) |
| `retrieve_similar_cases` | 案例库相似检索 |
| `reflect` | 档案级反思(校准笔记) |

风险档案结构:`risk_focus`(行业+主题)+ `trajectory`(按时间的
risk_level/score/trend 序列)+ 校准笔记。LTM 落盘在
`credit_risk/output/store*`(`store_dir` 参数控制)。

## 5. 常见改动入口

| 想改什么 | 去哪改 |
|---|---|
| 某 agent 的行为 | `agents.py` / `sourcing.py` 里的 prompt 常量 |
| 事件类型/严重度定义 | `skills/credit_risk_taxonomy/SKILL.md`(两个 pipeline 同时生效) |
| 评分段/判决映射 | `skills/risk_scoring_rubric/SKILL.md` |
| WoE 权重/灰区 | `evaluate.py` 顶部常量 |
| 债务人名单 | `ObligorRegistry` 或从 candidates.csv 加载 |
| 告警库结构 | `pipeline.py` 的 `AlertRepository` |
| 加数据源 | `sourcing.py` 加通道 + `ingest()` 分流 |

## 6. 已知约束

- 依赖 `credit_risk_demo.py` 的记忆助手和 SKILLS_DIR——两个文件必须保持
  同级目录(`build_pipeline` 按相对路径找 demo)
- `investigate` 故意不调 LLM,检索质量取决于 LTM 的 embedding 质量
- 8-K 正文进 detect 时截断到 2000 字符/份(`_process_group` 里的
  `text[:2000]`),长公告的关键信息如果不在前面会丢
- 告警检索 `alerts_for()` 是"相似度召回 + obligor_id 过滤",obligor 之间
  名字很像时可能串——必要时改成纯精确过滤

## 7. 下一步

- ~~`run_contemporary_eval.py`~~ 已完成(dev+test 13 样本:检出 7/8、
  平均提前 ~93 天、负例误报 1/6,详见
  `credit_risk/output/eval_contemporary/REPORT.md`)。后续:开
  `--with-text` 测全文通道增益;在 train 集(28 样本)上做优化实验。
- HITL 钩子接 GUI
- 拓扑搜索(AFlow)和四 agent 的 MIPRO 优化
