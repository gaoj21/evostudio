# 使用自己的输入数据集

1. 在画布左侧 Library 的 **Input sources** 中添加 **My dataset**。
2. 点击该 Input 节点，选择 **Upload dataset**，或选择之前上传的数据集。
3. 查看记录数、字段和前五条预览。长字段在预览中缩短显示，保存和运行的数据不截断。
4. 将 Input 连接到工作节点，在连接设置中映射输出字段到节点输入。如果需要改字段名，可展开 Input 的 **Field mapping**。
5. 单次运行读取一条记录（可通过现有运行界面选择记录）；Batch Run 选择 **Canvas source node config**，直接读取已保存记录，不需要 API 采集步骤。运行前会显示实际记录数。

## 格式

支持 UTF-8（允许 BOM）的 `.csv`、`.tsv`、`.json`、`.jsonl`。Excel 请先导出为 CSV。
文件大小和记录数默认不限；每份数据集最多 200 个字段。可配置部署级大小和记录数限制，超限时整体拒绝，不会截断后默默运行。

CSV / TSV 第一行为字段名，字段名必须非空且不重复，各行列数应与表头一致。
CSV / TSV 值保留为字符串；JSON / JSONL 保留数值、布尔值、对象等类型。

JSON 示例：

```json
[
  {"company": "Example A", "news": "Quarterly update", "revenue": 120},
  {"company": "Example B", "news": "Business update", "revenue": 95}
]
```

也接受 `{"records": [...]}`。JSONL 每行一个对象。不同记录缺失的字段在输入中补为 `null`。
嵌套对象不会自动展开，数据集也不会自动添加风险标签、划分 dev/test 或转换成 credit-risk 数据。

## 管理与运行

- 数据集跨任务复用，使用独立 ID 引用；改名不影响现有连接。
- **Record limit** 为 0 时读取全部，正整数时按文件原始顺序读取前 N 条。
- 字段映射修改后，需要更新受影响的下游连接；未知字段、空输出名和重复输出名会导致可见的运行错误。
- 删除需要在 Input 内确认。删除后其他引用该数据集的任务需重新选择输入；已保存的运行结果保留。
- 这些是通用输入记录。如需评测，可使用现有 Batch 的 metric / label field 设置；系统不会推断真值标签。

## 存储与接口

数据保存于 `backend/data/datasets/`，或 `EAX_STUDIO_DATA_DIR/datasets/`，不进入冻结的项目数据集目录。
画布仅保存数据集 ID、字段映射和读取上限。原始用户数据属于运行数据，不应提交到 Git。

- `GET /api/datasets`：已保存的数据集元信息。
- `POST /api/datasets`：multipart `file`，可选 `name`。
- `GET /api/datasets/{id}`：元信息与前五条预览。
- `PATCH /api/datasets/{id}`：`{"name": "新名称"}`。
- `DELETE /api/datasets/{id}`：删除已保存的数据集。
- 来源配置：`{"type":"user_dataset","dataset_id":"...","n":0,"field_mapping":{"原字段":"输出字段"}}`。

实现位于 `backend/features/data/user_datasets.py` 和 `frontend/src/features/data/DatasetInput.jsx`。
装配涉及 `backend/api/app.py`、`backend/features/data/{source_apis,sources}.py`、
`frontend/src/api.js`、`frontend/src/features/canvas/Inspector.jsx`。
不涉及 `backend/llm/`；公司侧模型接口可原样保留。

## 新闻 + 内部 obligor list 双输入

在画布放置两个 Input，都连接到需要这些信息的工作节点：

- 新闻 Input：使用新闻 API 或上传新闻数据集。上传数据集的 **Input role** 保持 **Per-record input**。
- 名单 Input：使用 **My dataset** 上传 obligor list，将 **Input role** 设置为 **Shared reference**，输出名默认 `obligor_list`。
- 给工作节点声明 `news`（或新闻来源的实际输出名）与 `obligor_list` 两个输入，并在两条连接中分别映射；在 prompt 中引用这些输入，说明匹配公司或筛选新闻的要求。

共享参考把所有选定行封装成一个列表，每条新闻都收到整份名单。3 条新闻 + 2 家公司是 3 次运行，而非按行配对或生成 6 个组合。
可以添加多份共享参考，每份使用不同的输出名。当前批量模式接受一个逐条来源和多个共享参考，不支持两个独立逐条来源的自动 join。

单次运行的记录选择针对新闻，参考名单总是完整传入。批量运行、先采集后运行及边采集边运行均支持此模式。
采集开始时固定参考名单快照；后续各批（含末尾不足一批的数据）使用同一版本。修改参考 Input 的配置后需要重新采集。
记录字段与参考输出重名时直接报错，避免覆盖。名单不做自动匹配或截断；匹配逻辑由连接到的节点/工具决定。

## 全量预处理后再分批

Run → Batch → Canvas source node config → Execution mode：

- **Run each batch as data arrives**：够一批就执行，沿用现有逐条预处理。
- **Preprocess ALL data, then run batches**：读取完全部数据与参考名单后，调用一次全量预处理工具，再按 Records per batch 顺序执行处理后的记录。
- **Collect everything, then run**：只先保存采集数据，之后手动启动普通 Batch Run。

全量预处理工具在 Custom 中创建，在 **Whole-dataset preprocessor** 中选择。函数接受一个记录列表，返回记录列表，可以过滤、去重、重排或派生字段。每条记录包含已连接的 `obligor_list` 等共享参考数据。
该模式使用选定的全量工具替代本次运行的逐条预处理，避免预处理重复执行；不会修改 workflow 保存的默认预处理配置。

例如保留第一次出现的新闻（实际匹配字段按自己的数据定义调整）：

```python
def prepare_all(records: list) -> list:
    """Deduplicate the complete input before running batches."""
    seen = set()
    result = []
    for record in records:
        key = record.get('news')
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(record)
    return result
```

工具必须保留下游需要的字段和共享名单。系统在任何批次启动之前校验完整处理结果；失败即停止。返回空列表表示全部过滤掉，正常完成但不执行 Agent。
界面分别显示采集条数、处理后条数和各批次结果。Stop 同时停止采集/预处理或当前批次；已经完成的结果保留。

## 精简界面入口

Input 默认只显示 Choose data 和 How to use it。Preview data、Advanced settings（记录上限、输出名、字段映射）和 Manage dataset（改名、删除）按需展开。
Batch Run 默认使用画布输入；数据源切换、Run details、Evaluation 和 Parallel runs 默认折叠。底部只显示当前步骤需要的主按钮：采集、普通运行或全量预处理后运行。

## 通用字段与共享资源（2026-09-14）

JSON 列类型根据实际值显示；混合类型标为 any。CSV 默认保留文本，可在 Advanced settings → Field mapping 中为各列选择 int、float、bool、list、dict 或 str。转换只作用于当前 Input；非文本类型的空值变为 null，非法值以具体行号/列名报错，原始数据集不变。

Manage dataset 中的 Detach from this Input 只解绑当前节点。删除共享数据集前必须从所有引用它的已保存 Task 中解绑并保存；否则返回使用列表并保留文件。

JSON 上传按内容识别：支持对象数组、`{"records": [...]}`、单个对象、逐行或连续的多个完整对象；`.json` 与 `.jsonl` 均可。文件尾部存在损坏内容时整份拒绝并报告行列位置，不会悄悄导入有效前缀。此解析规则也用于 Batch/Evaluation 文件上传。

## 上传大小限制

默认取消文件大小和记录数限制，不再固定为 20 MB / 50,000 行。可选环境变量 `EVO_DATASET_MAX_BYTES`（字节）与 `EVO_DATASET_MAX_ROWS`（记录数）设置部署限制，0 表示不限；修改后重启服务。CSV 长字段不再受解析库默认字段长度限制。解析和保存放在线程池执行，避免占用 API 主事件循环。

当前数据集仍采用整份解析与 JSON 持久化，并非流式数据库；可处理大小仍取决于机器内存、磁盘和反向代理配置。
