# LLM 适配层：更换 API 从这里开始

所有平台模型创建入口集中在本目录。API 密钥通过环境变量读取，不能写进代码、提示词或上下文包。

| 文件 | 修改场景 |
| --- | --- |
| `registry.py` | provider 配置加载、默认 provider、密钥读取 |
| `providers.json` | 现有供应商地址、模型、参数；不要把密钥写进去 |
| `client.py` | 普通聊天公共重试策略；返回文本 |
| `factory.py` | EvoAgentX 模型入口；分派到适配器 |
| `agent.py` | Deep Agents / LangGraph 模型入口 |
| `adapters/__init__.py` | 内置类型映射与自定义模块加载 |
| `adapters/openai_compatible.py` | OpenAI 协议请求、返回解析和两类框架客户端创建 |
| `adapters/litellm.py` | LiteLLM 请求和两类框架客户端创建 |

## 只换地址/模型

保留现有 type，修改 `providers.json` 中对应项的 `base_url`、`model`、`api_key_env`、`params`。
注意 LiteLLM 类型由模型前缀及 SDK 决定路由；使用自定义 OpenAI-compatible host 时用 `openai_compatible`，不要假设 LiteLLM 任意 provider 都采用同一个 base URL 规则。

## API 协议完全不同

新建 `backend/llm/adapters/my_vendor.py`。配置示例（不要覆盖现有完整配置）：

```json
{
  "default": "my_vendor",
  "providers": {
    "my_vendor": {
      "type": "custom",
      "adapter": "llm.adapters.my_vendor",
      "model": "your-model-id",
      "api_key_env": "MY_VENDOR_API_KEY",
      "enabled": true,
      "params": {"timeout": 60}
    }
  }
}
```

模块按需要实现三个函数；未实现的能力会明确报错，不会偷偷回落到别的 API：

```python
def chat(cfg: dict, messages: list, kwargs: dict) -> str:
    # 把 messages 转成新 API 请求；发送；解析成助手文本。
    # 使用 cfg['api_key']，不要从业务代码再获取密钥。
    # 尊重 kwargs/params 中的 timeout；HTTP 异常保留 status_code/code。
    ...

def create_workflow_model(cfg):
    # 返回 EvoAgentX BaseLLM 实例；支持 generate / async_generate、config、
    # 结构化输出解析，以及框架复制模型配置的行为。
    ...

def create_agent_model(cfg):
    # 返回 LangChain BaseChatModel；必须支持 bind_tools 与工具调用消息，
    # 正确保留 tool-call ID、tool name、arguments 和 ToolMessage。
    ...
```

上面是合同说明，不是可以直接运行的实现。参考同目录两个已运行的适配器。
为新协议实现 BaseLLM 时参考 `backend/evoagentx/models/base_model.py`、`model_configs.py` 及具体模型类。
`create_agent_model` 需要对应的 LangChain provider SDK 或自定义 BaseChatModel；不能用普通文本函数替代 tool calling。

## 哪些功能会用它

- Chat / 结果聊天的隔离 worker → `chat`；停止通过终止 worker 中断请求。
- workflow runner、工作流生成、历史 MIPRO、离线 Evolve 建议 → `get_evoagentx_llm`。
- 独立 Chat agent 和 Deep Agents harness → `get_agent_model`。
- prompts、Memory 权限、工具选择、数据标签仍由 feature 模块处理，不应搬进 adapter。
- 原框架和 projects 里的独立研究脚本可能直接调用 `evoagentx.models`，它们不等于平台入口；若要迁移这些脚本，按脚本入口单独检查。

## 验证

```sh
.venv/bin/python -m pytest tests/src/test_llm_factory.py tests/src/test_llm_adapters.py tests/api/test_harness.py -q
```

先用假的响应验证格式、错误、三种分派和 tool calls，再明确发起一次小请求。
不要运行全量 Batch/Evolve 来检查 API 是否接通；不要在测试输出中打印 key 或完整 provider 配置。
