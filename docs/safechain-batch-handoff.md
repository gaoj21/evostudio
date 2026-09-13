# 公司侧 SafeChain 批量接口交接

本次只修改 LLM 之外的前端、运行调度和参数透传，未修改 `backend/llm/`。
真正向 SafeChain 提交原生批量请求，由公司侧 LLM 适配实现。仅接收参数但继续发单条 HTTP 请求，不算完成原生批量接入。

## 使用入口与参数

Run → Batch → Model execution → **SafeChain / native batch API**，设置 **API batch size**（1–1024）。
请求字段为 `llm_batch_size`；普通 JSON 输入、上传文件 multipart、canvas 输入与采集接口均支持。
留空/不传则保持原有 Standard API 路径。SafeChain 模式不显示手动 Workers 设置。

两种 size 不同：

- `batch_size`（采集接口）：每一业务批次包含多少输入记录。
- `llm_batch_size`：送往模型入口的 API 请求批量上限。

## 需要公司侧修改的公开函数

保持原有返回类型与不传参数时的行为，给已有入口增加两个可选关键字参数：

```python
def get_evoagentx_llm(provider=None, *, batch_size=None, batch_id=None):
    # 返回已有 EvoAgentX 模型对象。
    # 把 batch_size 接入 SafeChain 原生批量能力。
    ...


def get_agent_model(provider=None, *, batch_size=None, batch_id=None):
    # 若 workflow 使用 Deep Agents，返回已有 LangChain 模型对象。
    ...
```

外层实际调用：

```python
model = get_evoagentx_llm(batch_size=16, batch_id="workflow-batch-id")
```

- `batch_size` 是最大请求数，不能要求每次必须凑满。
- `batch_id` 标识同一个 Studio 业务批次，用于共享 SafeChain 客户端/请求队列及隔离不同批次。它不是单条请求 ID。
- 同一批次里的多个 run 会分别创建模型；工作节点还会通过 `llm.config` 再构造框架 Agent。必须让 batching 配置保留在公司模型配置/适配器中，并让这些实例共享合批机制，而不是每条记录各建一个互不相通的队列。
- 如果 SafeChain 本身通过共享客户端自动合批，请直接复用该机制，不需要在外层重复实现 HTTP batching。
- 需要为每次模型调用匹配正确的返回结果；工具调用及多轮 Agent 的返回合同保持原样。
- 当待处理请求不足 `batch_size` 时，需要以有界等待/自动 flush 发出。末尾批、串行 trajectory 和工具分支都可能不足一批。不能永远等满，否则会死锁。
- 独立 Chat 的普通模型调用没有传这两个参数，因此仍应正常工作。

当前 LLM 入口未提供这些关键字参数时，原生批量模式在运行前给出 `Native API batching is not connected yet`，不会调用模型或退回单条模式。
这个检查只确认函数签名，不能证明公司实现真正发出了原生批量请求；接入后请通过 SafeChain 日志确认。

## 外层保留的调度逻辑

workflow 仍要维护每条记录的节点进度、memory、工具调用及结果，因此保留内部并发调度来让独立记录的请求进入 SafeChain。原生模式自动设置并发上限为 `min(batch_size, 64)`，不再与用户手填 Workers 叠加。
这不是原生合批的实现；真正合批在公司侧 LLM/SafeChain 内完成。依赖 memory 的同一 trajectory 仍按时间顺序执行，不能为了凑满 API 批次并行后续时间点。
因此实际 API 请求数可以少于设置的上限，尤其设置超过 64 或只有少量独立 trajectory 时。

批次保存 `llm_batch_size`；每条 run 收到相同 `batch_id` 和大小，重试/续跑沿用保存的设置。
流式和全量预处理后的每个业务批次使用独立的 `batch_id`。预处理工具自身不使用此模型参数。

## 外层代码位置

- `backend/features/execution/provider_batch.py`：参数校验、入口签名检查、模型参数转换。
- `backend/features/execution/batch.py`：自动调度数量、保存大小、向 run 传递参数。
- `backend/features/workflow/runner.py`：传给 EvoAgentX 模型入口。
- `backend/features/agents/harness.py`：传给 Deep Agents 模型入口。
- `backend/features/data/source_collection.py`：流式及全量预处理模式的透传。
- `backend/api/app.py`：普通批量/文件上传的参数接收。
- `frontend/src/features/execution/RunDialog.jsx`：模式和大小设置。

## 公司侧验证

1. 两条独立记录、size=2：通过 SafeChain 日志确认原生批量提交及响应一一对应。
2. 三条独立记录、size=2：最后一条必须返回，不等待凑满。
3. 同一公司多个日期、size=16：每个日期仍依赖前一个日期完成，不能卡住。
4. 包含工具调用的节点：工具结果后的下一次模型请求仍能返回正确 run。
5. 失败重试/续跑：大小保留，已经成功的记录不重新执行。

本地验证使用模拟模型入口，不访问公司服务，不验证 SafeChain 内部实现。
