# Mem0 接入说明

## 已实现

使用 `mem0ai==2.0.20` 开源 Python SDK。平台继续使用现有执行器，Mem0 管理独立的共享记忆空间。这里的“项目隔离”指平台内数据范围约束，不是多用户认证系统。

- 同项目任务可选择同一空间；不同项目不可访问该空间，未归类任务各自隔离。
- Agent 执行前从选定空间检索，整轮执行结束后写入所选输入/输出；沿用独立读写开关。
- UI 支持创建空间、浏览、搜索、添加、编辑、删除记忆。运行结果展示 Mem0 召回条数和写入情况。
- 默认 `infer=False`，原文保存，不触发额外的 LLM 提取。SDK 初始化了 Ollama 客户端以满足配置要求，但当前路径不调用它，无需启动 Ollama 服务。
- 使用本地 HuggingFace `BAAI/bge-small-en-v1.5`、384 维向量。Qdrant 本地磁盘存储，SQLite 保存 Mem0 编辑历史。未启用模型重排、BM25 或图记忆。
- 默认禁用 Mem0 telemetry。模型使用 `local_files_only=True`；本机缓存缺失时明确报错，不自动联网下载。

## 使用

1. 打开已保存任务的画布，选中 Agent，开启 Long-term memory。
2. Memory backend 选择 **Mem0 shared space**，创建或选择一个空间。
3. 勾选读取/写入，选择要保存的字段及召回数量。新空间创建本身不会写入内容或调用模型。
4. 另一个同项目任务选择同一空间，即可共享；只使用历史的 Agent 关闭写入。
5. 打开 Memory 面板，展开 **Mem0 shared memory**，选择空间进行管理。

读取只发生在 Agent 执行时。当前轮上游结果仍通过工作流输入输出传递；长期记忆在整轮结束后保存。手动输入的内容和 Agent 自动记录共用同一 Mem0 空间。首次操作需要加载本地嵌入模型，会比之后慢。

## 依赖与存储

依赖在 pyproject.toml 的 `mem0` 可选组中；现有运行环境已安装并完成真实 SDK 验证。新环境安装 `mem0ai==2.0.20`、sentence-transformers、ollama，并预先缓存上述嵌入模型。

所有平台 Mem0 数据位于 `backend/data/mem0/`，或 `EAX_STUDIO_DATA_DIR/mem0/`：

- `spaces/`：空间 ID、名称与所属范围。
- `vectors/`：持久化 Qdrant 数据。
- `history.db`：编辑历史。

目录已加入 gitignore。默认配置适合当前单服务进程；不要让多个服务进程同时打开同一个本地 Qdrant 目录。多进程部署需后续改接独立 Qdrant 服务。

## API

以 `/api/graphs/{graph_id}/mem0` 为前缀，服务端从保存的 graph 推导所属项目：

| 操作 | 路径 |
|---|---|
| 列表与组件状态 | GET `/spaces` |
| 创建空间 | POST `/spaces`，`{name}` |
| 浏览/检索 | GET `/spaces/{space_id}/entries?q=...&limit=100` |
| 添加原文 | POST `/spaces/{space_id}/entries`，`{content}` |
| 编辑 | PUT `/spaces/{space_id}/entries/{memory_id}`，`{content}` |
| 删除 | DELETE `/spaces/{space_id}/entries/{memory_id}` |

更新和删除前会核对 entry 的 Mem0 user_id 与空间 ID，避免凭另一个空间的记录 ID 跨空间修改。Agent 运行计划同样校验空间归属。接口最多返回 1000 条，前端默认显示 100 条，当前没有翻页功能。

## 兼容与边界

- 现有节点记忆、会话历史、Credit Risk 对象时间表均保留；不自动迁移或改变已有任务。
- Mem0 当前承担通用语义记忆。按公司、观测时点做精确历史截断的轨迹记录继续使用 table。
- 此轮没有自动提取开关、空间删除、迁移导入或历史回滚 UI。
- 绑定共享 Mem0 的任务暂不支持独立 Python 项目导出，接口会明确拒绝，避免导出后悄悄使用另一套存储。
- 工作流 reset 不清理共享 Mem0；绑定 Mem0 的任务 reset 会拒绝并提示为干净实验新建空空间，避免误清其他任务使用的历史。全局 reset 在存在共享空间时也会拒绝。
- 将任务移动到另一项目后，其旧空间绑定会校验失败；需显式选择新项目空间。原空间保留。

## 验证

真实 SDK + 临时磁盘 Qdrant + 本地嵌入模型验证了写入、跨任务读取、检索、编辑、删除及项目隔离；提取模型方法被替换成“一旦调用就失败”，验证过程中未触发。测试内容未写入平台正式数据目录。自动化测试另覆盖 API 归属校验、跨空间 ID 修改拒绝、运行计划校验、运行器自动保存与召回、前端创建和添加、失败时保留草稿。

官方参考：
- https://github.com/mem0ai/mem0
- https://docs.mem0.ai/open-source/configuration

## 画布资源与连线升级

- 节点库增加 `+ Add shared memory`，可在没有 Agent 绑定时将空间加入画布。同一 space_id 只显示一个资源节点。
- Memory → Agent 开启读取；Agent → Memory 开启写入。新 Agent 只开启这次连接请求的方向。读取数量为 0 时重新连入会恢复为 3。
- 可以选择连线后按 Delete/Backspace 断开，也可在资源检查器逐条断开；另一方向保持不变。
- 点击资源打开独立检查器，展示读者/写者、增加连接、进入 Agent 设置和管理该空间的条目。
- 不允许 Memory → Memory、工具/数据源 → Memory，以及其他 Agent 写入旧节点私有存储。尝试替换仍在使用的另一个空间或不兼容存储会显示原因，不静默改绑。
- 资源可以拖动。资源列表与位置分别保存在 graph.memory_resources 和 graph.memory_positions，参与脏状态与撤销重做，不进入工作流 tasks/edges，也不改变执行顺序。
- 断开最后一条连线仍保留资源；“Remove from this canvas”解除当前工作流绑定并移除展示，不调用 Mem0 删除接口。删除 Agent 会清理指向它的旧记忆读取引用，共享空间继续保留。
- 独立资源化优先应用于 Mem0。原来的表格/向量节点存储仍归属其原 Agent，独立检查器允许连接读取与跳转修改存储设置。
