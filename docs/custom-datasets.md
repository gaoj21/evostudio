# 使用自己的输入数据集

1. 在画布左侧 Library 的 **Input sources** 中添加 **My dataset**。
2. 点击该 Input 节点，选择 **Upload dataset**，或选择之前上传的数据集。
3. 查看记录数、字段和前五条预览。长字段在预览中缩短显示，保存和运行的数据不截断。
4. 将 Input 连接到工作节点，在连接设置中映射输出字段到节点输入。如果需要改字段名，可展开 Input 的 **Field mapping**。
5. 单次运行读取一条记录（可通过现有运行界面选择记录）；Batch Run 选择 **Canvas source node config**，直接读取已保存记录，不需要 API 采集步骤。运行前会显示实际记录数。

## 格式

支持 UTF-8（允许 BOM）的 `.csv`、`.tsv`、`.json`、`.jsonl`。Excel 请先导出为 CSV。
每个文件最多 20 MB、50,000 条记录、200 个字段；超出限制会报错，不会截断后默默运行。

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
