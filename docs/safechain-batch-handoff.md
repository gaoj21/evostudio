# SafeChain 批量调用：Studio 外层接入

模型边界是仓库根目录的独立包 `llm/`（由公司维护，契约见 [../llm/README.md](../llm/README.md)）。旧的 `backend/llm/` 已删除。Studio 直接使用它的公开入口，不再要求模型工厂接受 `batch_size` 或 `batch_id`：

```python
from llm import batch
results = batch("safechain", [
    [{"role": "system", "content": "..."}, {"role": "user", "content": "第一条请求"}],
    [{"role": "system", "content": "..."}, {"role": "user", "content": "第二条请求"}],
])
```

外层列表是独立请求，每个元素是完整消息历史。返回列表与输入等长且顺序一致，每项为字符串或 LangChain `AIMessage`。Deep Agents 输入为 LangChain 消息对象列表，工具调用输出保留 `AIMessage.tool_calls`。

## 运行入口

Run → Batch → Model execution → SafeChain / native batch API，设置 API batch size（1–1024）。标准模式不受影响。

- `llm_batch_size`：一次调用 `llm.batch` 的最大请求数。
- 数据采集的 `batch_size`：每批送入 Workflow 的业务记录数，含义不同。
- 内部并发最多 `min(llm_batch_size, 64)`，用于让独立 Workflow 请求进入队列。不是原生批量接口的替代品。
- 同一 trajectory 仍按顺序运行，不为了凑批并行后续日期。

## 外层行为

`backend/features/execution/provider_batch.py` 按业务 batch ID、批量上限和模型调用选项隔离队列。达到上限立即派发；从第一条请求起最多等待 50ms 后派发剩余请求。因此最后不足一批、只有一条独立 trajectory 时也可以继续。

响应按位置交还各自节点。数量不符或整批异常会让该批等待者收到错误；结果列表中的单项异常只影响对应请求。尚未派发的已取消请求不会提交；已经提交的公司 API 调用无法仅靠外层取消撤回。

框架在创建 Agent 时会重新构造模型，外层在构造完成后替换模型的生成传输方法，保留消息准备和输出解析。AIMessage 原生工具调用转为 EvoAgentX 的工具调用格式；Deep Agents 则直接保留消息对象、工具调用 ID 和消息历史。普通 Chat 未切换到此批量队列。

## 工具与选项

普通文本请求只调用两个位置参数。工具节点还需要入口支持 `tools`、`tool_choice` 等 LangChain 模型选项，例如 `batch("safechain", inputs, tools=schemas)`。外层会透传这些选项，并把不同选项的请求分开合批。若公司入口仅支持两个位置参数，文本请求可用，工具绑定会明确报错，不会默默丢弃工具定义。

框架专用 `enable_prompt_caching` 提示不会传给 SafeChain；值为 None 的选项省略。模型和默认温度等配置继续由公司 `llm.batch` 管理。

本地未安装公司版 `llm.batch` 时，原生模式启动前明确报错，不退回单条 API，也不修改 `llm` 文件夹。

## 相关模块与验证

- `backend/features/execution/provider_batch.py`：共享合批、响应转换、框架与 LangChain 适配。
- `backend/features/workflow/runner.py`：在 Agent 构造后接入批量传输。
- `backend/features/agents/harness.py`：Deep Agents 接入。
- `backend/features/execution/batch.py`、`backend/features/data/source_collection.py`：批量大小、采集流程、重试和续跑透传。
- `tests/studio/test_provider_batch.py`：合批上限、尾批、取消、错误、隔离、工具消息以及框架重建后实际执行路径。

本地验证使用模拟的公开 batch 函数，不调用公司服务。公司环境仍需通过 SafeChain 日志确认真实原生请求，以及工具参数是否被入口支持。

带工具的工作流现在会在批量运行前检查 batch 入口是否接受 tools 参数；不满足时立即提示使用 Standard 或完善公司适配，而不是等到节点执行时才失败。
