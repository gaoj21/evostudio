# Studio Run、输入与 Flow 执行重构计划

## 1. 背景

当前 Studio 的运行链路由三套规则共同决定：

1. React Flow 画布保存用户绘制的节点和连线；
2. Studio 后端根据画布连线计算拓扑顺序和运行输入；
3. EvoAgentX `SequentialWorkFlowGraph` 再根据节点输入、输出的同名字段重新推断实际数据流。

三套规则没有共享同一个执行计划，导致以下问题：

- 画了连线但字段名不同，实际运行时两个节点没有数据依赖；
- 没有画连线但字段名相同，实际运行时会自动形成依赖；
- Tool 节点无论画在什么位置，都先于全部 LLM 节点执行；
- Run 弹窗可能显示已保存 Flow 的旧输入，提交时却运行刚保存的新 Flow；
- `Start from` 会列出未连线、只有 Source、实际无法运行的节点；
- Source 配置多条记录时，Single run 与 Batch run 的数量语义不清楚；
- 启动失败时弹窗关闭，输入和错误上下文分离。

本计划的目标是让用户看到的 Flow 成为唯一执行事实来源，并让输入预览、启动请求和实际执行使用同一个不可变计划。

---

## 2. 目标原则

### 2.1 单一事实来源

- 节点是否执行、先后关系和可达范围只由画布节点与边决定。
- 输入字段由进入当前执行范围的边映射推导。
- 不再在运行阶段依据全局同名字段创建画布上不存在的连接。

### 2.2 先计划，后运行

每次运行必须经历：

```text
当前画布快照
  → 校验
  → 生成 Execution Plan
  → 展示输入与运行规模
  → 用户确认
  → 使用同一个 Plan 启动
```

弹窗展示的输入、Source 数量、起点和最终运行内容必须来自同一个 `plan_id` 或 `graph_revision`。

### 2.3 Node 类型统一编排

Source、Tool、LLM 都是执行节点，只是执行器不同：

| 类型 | 执行器 | 主要输出 |
| --- | --- | --- |
| Source | Source adapter | 一个输入记录或一组记录 |
| Tool | Deterministic tool adapter | 工具返回值 |
| LLM | EvoAgentX agent adapter | 结构化或文本输出 |

它们必须在同一个拓扑计划中按依赖执行，不允许先把某一类节点整体抽出。

### 2.4 显式状态，不使用隐式猜测

- 节点是否参与运行使用 `enabled` 或明确的 Disabled 状态。
- 未连线节点不再因为画布中出现其他边而自动变成 parked。
- Source 是单条还是多条必须在运行计划中明确。

---

## 3. 目标数据模型

### 3.1 Flow Edge

第一阶段可以继续使用同名字段自动建立映射，但映射必须固化到执行计划中：

```json
{
  "source": "extract",
  "target": "decide",
  "mappings": [
    { "from": "finding", "to": "finding" }
  ]
}
```

规则：

- 创建边时，自动列出上下游同名字段作为默认 mapping；
- 没有可映射字段的边只能被明确标记为 `control_only`；
- 普通数据边没有 mapping 时禁止保存；
- 一个必填输入只能有一个明确生产者；
- 工作流外部输入不需要伪造 Source 节点，由 plan 的 `inputs` 表示。

### 3.2 Execution Plan

建议新增后端统一结构：

```json
{
  "plan_id": "sha256:...",
  "graph_id": "credit-risk-monitoring",
  "graph_revision": "updated_at-or-content-hash",
  "mode": "single",
  "start_at": null,
  "nodes": [
    {
      "name": "feed",
      "kind": "source",
      "depends_on": [],
      "input_bindings": {},
      "output_names": ["company", "news_batch"]
    }
  ],
  "inputs": [],
  "source": {
    "node": "feed",
    "cardinality": 4,
    "single_policy": "first"
  },
  "warnings": []
}
```

要求：

- 输入弹窗只读取 Execution Plan；
- Run 请求提交 `plan_id + inputs`；
- 后端启动前确认 graph revision 未变化；
- plan 失效时返回 `409 plan_stale`，前端重新规划，不允许静默使用新 Flow。

### 3.3 Run 状态

统一单次与批量运行的状态名称：

```text
planning → ready → starting → running
                         ├→ succeeded
                         ├→ failed
                         ├→ cancelling → cancelled
                         └→ abandoned
```

批量运行额外提供聚合结果：

- `succeeded`
- `completed_with_errors`
- `cancelled`
- `failed`

不能再把存在失败记录的 Batch 显示成绿色 `completed`。

---

## 4. 分阶段实施

## Phase 1：修正 Run 与输入契约

目标：暂时不替换底层执行器，先保证用户确认的内容就是实际启动的内容。

### 后端

- 新增 `POST /api/graphs/{graph_id}/run-plan`。
- 请求可接收当前画布快照，或要求前端先保存并提交 revision。
- 返回：
  - 可运行节点；
  - 合法的 `start_at`；
  - workflow inputs；
  - Source 数量和 Single/Batch 提示；
  - validation errors 和 warnings；
  - `plan_id`。
- `POST /run` 改为接收 `plan_id`，启动前检查 revision。
- 在返回 `run_id` 前完成：
  - start point 校验；
  - 输入缺失和类型校验；
  - Tool/Source 可用性校验；
  - preprocess 校验。
- 即使输入对象为空，也必须执行配置的 preprocess。

### 前端

- 点击 Run 后先进入 `planning`，成功后才显示表单。
- 删除 `workflowInputs` 旧状态与弹窗内第二次独立推导之间的双来源。
- planning 期间禁用提交。
- 启动失败时保留弹窗、输入值和当前模式。
- `startRun` 必须向调用方返回明确结果：

```ts
{ ok: true, runId }
{ ok: false, error }
```

- Chat 只有获得真实 `runId` 后才能显示 started。
- 对 plan 请求使用 AbortController 或递增 request token，忽略过期响应。

### Phase 1 验收标准

- 修改节点输入后点击 Run，弹窗立即显示新字段。
- Run 弹窗展示后再启动，后端执行的 graph revision 与弹窗一致。
- 保存、验证或启动失败时弹窗不关闭。
- Chat 不会在没有 `run_id` 时显示 started。
- 不可运行节点不会出现在 Start from。

---

## Phase 2：重做输入体验

目标：让用户清楚知道“一次运行会收到什么、会执行多少次”。

### Single run

- 没有 Source：展示外部输入表单。
- Source 只产生一条：显示来源和记录摘要。
- Source 产生多条：必须明确选择：
  - Run first record；
  - Choose a record；
  - Run all as batch。
- 不再默认静默取第一条。
- `Start from` 只展示至少能到达一个可执行 LLM/Tool 节点的起点。
- 起点变更后显示：
  - 将执行的节点；
  - 将跳过的节点；
  - 需要人工补充的字段；
  - 来自历史 Run 的预填字段及其 revision。

### 类型输入

- Optional boolean 使用三态：unset / true / false。
- `dict` 和 `object` 拒绝 JSON `null`。
- Optional string 空值默认不提交；如需空字符串，提供显式方式。
- 类型错误同时在字段旁和顶部摘要展示。
- 历史输入缓存绑定 `graph_id + graph_revision + start_at`，避免 Flow 改版后复用旧语义。

### Batch run

- 所有 Source 类型在预览前使用同一个 Execution Plan。
- Preview 请求必须包含 metric、label key、workers 和 Source 参数。
- Preview 失败或仍在计算时禁止 Run batch。
- 显示实际总运行数和预计节点执行次数：

```text
4 records × 7 nodes = up to 28 node executions
```

- 空数据集禁止启动。
- 明确说明取消只能阻止尚未开始的记录。

### Phase 2 验收标准

- Source `n=4` 不会再被误认为一次 Single run 会跑四条。
- 快速切换 Start from 不会出现旧响应覆盖新响应。
- Preview 报错时启动按钮不可用。
- Optional boolean 可以保持未提供状态。
- Evaluation 缺少 label 字段会在预览阶段发现。

---

## Phase 3：统一 Flow 执行引擎

目标：画布显示、输入推导和实际运行共享一个 DAG。

### Graph 编译

新增 `compile_execution_plan(graph, start_at=None)`，负责：

1. 验证节点名称和边；
2. 解析 edge mappings；
3. 计算每个节点的直接前驱；
4. 计算外部输入；
5. 计算可执行节点和拓扑层级；
6. 校验必填输入是否有唯一来源；
7. 生成稳定的 graph revision 和 plan id。

`compute_workflow_inputs`、`subgraph_from`、Run preview 和 runner 都必须调用这一编译结果，不能各自重新推导。

### 混合节点执行

按拓扑层级运行：

```text
ready nodes
  ├→ Source executor
  ├→ Tool executor
  └→ LLM executor
       ↓
merge declared outputs through edge mappings
       ↓
unlock downstream nodes
```

- Tool 可以出现在 LLM 前后任意合法位置。
- 同一拓扑层、互不依赖的节点可以后续增加并发；第一版先串行保证正确性。
- Tool 多输出必须按声明字段映射：
  - 返回 dict 时按 key 提取；
  - 单输出时允许把整个返回值绑定到唯一输出；
  - 多输出但缺少 key 时立即失败并指出缺失字段。
- Source 输出只流向有边连接的目标，不再合并进全局字典污染其他分支。
- LLM 只读取 workflow inputs 和直接/间接前驱通过映射提供的数据。

### 节点启用状态

- 增加 `enabled: true|false`。
- 删除“只要图中存在边，所有孤立节点都自动 parked”的规则。
- Disabled 节点在画布上明确显示，并且不参与 plan。
- 多个独立子图必须明确选择入口或分别运行，不能因是否存在一条无关边而改变行为。

### EvoAgentX 适配

优先方案：Studio 编译完整 DAG，LLM 节点继续复用 EvoAgentX Agent/Action 执行能力，但调度由统一 Execution Plan 驱动。

备选方案：将 Source 和 Tool 包装成 ActionGraph 节点，再构建带显式 edges 的 `WorkFlowGraph`。实施前需要验证：

- Source/Tool 的结构化输出是否能完整进入 Environment；
- 显式 control edge 是否会被调度器保留；
- 节点级状态与取消语义是否能暴露给 Studio。

### Phase 3 验收标准

- 画布无连线但字段同名的节点不会自动建立依赖。
- 画布连接但没有字段 mapping 时不能保存或运行。
- `LLM → Tool → LLM` 可以按画布顺序执行。
- 分支数据不会泄漏到未连接分支。
- 添加一条无关边不会让其他孤立节点突然停止执行。
- Start from、输入表单、节点状态和最终结果全部来自同一 Execution Plan。

---

## Phase 4：运行状态与错误呈现

### 后端

- Run 状态持久化包含 `plan_id`、graph revision 和输入摘要。
- 优先保存 `displayable_error`，完整 traceback 单独放入 `debug_error`。
- 节点错误结构统一：

```json
{
  "code": "missing_input",
  "node": "decide",
  "field": "context",
  "message": "decide requires context",
  "debug": "..."
}
```

- Batch 聚合状态区分全部成功、部分失败、启动失败和取消。

### 前端

- Canvas badge、Drawer 和 History 使用同一状态映射。
- `abandoned`、`cancelled`、`lost` 都可以打开详情。
- 错误默认显示可读摘要，提供 Show technical details 展开 traceback。
- 单次运行无法真正取消时，按钮使用 `Stop watching` 或 `Leave run`，避免让用户误以为计算会停止。

### Phase 4 验收标准

- 部分记录失败的 Batch 不显示绿色成功状态。
- abandoned/lost Run 可以查看原因和已有输出。
- 用户首先看到节点、字段和可执行建议，而不是 Python traceback。

---

## 5. 测试计划

## 5.1 Graph 编译单元测试

必须新增以下场景：

1. 有边且同名字段：生成 mapping 并执行；
2. 有边但字段不匹配：编译失败；
3. 无边但字段同名：不产生依赖；
4. 一个必填输入存在两个生产者：编译失败；
5. control-only edge：只约束顺序，不传输字段；
6. Source → LLM；
7. Source → Tool → LLM；
8. LLM → Tool → LLM；
9. 两条并行分支汇合；
10. Disabled 节点；
11. 多个独立子图；
12. Start from 后重新计算输入。

## 5.2 Run API 测试

- plan 与 run 使用相同 revision；
- stale plan 返回 409；
- 缺失输入在创建 run_id 前返回 422；
- 非法 start point 在创建 run_id 前返回 422；
- 空输入仍执行 preprocess；
- Source 多记录的 Single policy 必须显式；
- Tool 多输出正确拆分；
- displayable error 与 debug error 分离。

## 5.3 前端测试

- Dirty canvas 打开 Run 时使用当前输入 schema；
- plan 失败不打开输入表单；
- run 失败不关闭弹窗；
- 快速切换 start point 丢弃过期请求；
- Chat 没有 run id 时显示失败而非 started；
- Source 多记录提供 Single/Batch 明确选择；
- Preview 错误禁用 Batch Run；
- optional boolean 三态；
- abandoned/cancelled/lost 详情可打开。

## 5.4 端到端验收 Flow

至少维护四个不调用真实付费模型的固定工作流：

1. `Input → Tool → Output`
2. `Input → Mock LLM → Tool → Mock LLM`
3. `Source → Two branches → Join`
4. `Batch source → Tool → Evaluation`

端到端测试必须断言执行顺序、每个节点收到的输入、输出映射和最终状态。

---

## 6. 兼容与迁移

### 旧 Edge

旧图只有 `{source, target}`：

- 如果上下游有且只有一组同名字段，自动生成 mappings；
- 如果没有同名字段，迁移为 `control_only` 并展示 warning；
- 如果映射存在歧义，标记为 Needs attention，禁止运行但允许继续编辑和保存草稿。

### Parked 节点

- 第一次加载旧图时，将当前推导出的 parked 节点迁移为 `enabled: false`；
- 后续不再根据边数量动态计算 parked。

### 历史 Run

- 没有 plan id 和 revision 的历史记录继续可读；
- 标记为 Legacy run；
- 不允许直接用 Legacy run 的输出自动预填新版 Flow，除非字段和 revision 兼容。

### API 过渡

- 保留旧 `/run` 请求一个迁移周期；
- 旧请求在服务端即时生成 plan，并记录 deprecation warning；
- Studio 前端只使用新 plan API。

---

## 7. 推荐提交拆分

1. `test(studio): capture run and flow semantic gaps`
2. `feat(studio): add immutable execution plans`
3. `fix(studio): keep run dialog on planning and launch errors`
4. `fix(studio): derive inputs and start points from run plans`
5. `fix(studio): make batch preview authoritative`
6. `refactor(studio): compile canvas edges into data bindings`
7. `refactor(studio): execute source tool and llm nodes topologically`
8. `feat(studio): add explicit disabled node state`
9. `fix(studio): unify run and batch outcome states`
10. `docs(studio): document run input and flow semantics`

每个提交必须保持现有测试通过，并补齐该阶段暴露的新行为测试。

---

## 8. 实施顺序与停止点

建议严格按以下顺序推进：

1. 先写失败测试，固定当前已复现的问题；
2. 完成 Phase 1，暂停并做一次真实 Run 验收；
3. 完成 Phase 2，确认输入体验后再动执行引擎；
4. Phase 3 先支持串行混合 DAG，正确后再考虑并行；
5. 完成旧图迁移和四个固定 E2E Flow；
6. 最后统一状态和错误展示。

Phase 1 与 Phase 2 属于可控修复；Phase 3 是执行语义变更，必须单独提交、单独迁移、单独验收，不能和界面调整混在同一批改动中。

---

## 9. 完成定义

只有同时满足以下条件，重构才算完成：

- 用户看到的每条边与实际执行依赖一致；
- Run 表单展示的 schema 与实际运行 schema 一致；
- Source 的记录数量在确认前明确；
- Tool 能在 Flow 任意合法位置执行；
- Start from 不提供必然失败的选项；
- 启动失败不会丢失输入；
- 所有运行状态都有可查看的结果或原因；
- 旧工作流完成显式迁移且不存在静默语义变化；
- 单元、API、前端和固定 E2E Flow 全部通过。
