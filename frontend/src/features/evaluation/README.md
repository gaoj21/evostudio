# Evaluation / Evolve 前端

评测不在画布上：画布没有 evaluator 节点，评测代码写在 Evaluation & Evolve 弹窗里。

- `EvolvePanel.jsx`：弹窗外壳、历史列表、轮询、以及 Evolve / Evaluation code 两个视图的切换。每个 workflow 只有一份评估代码（在 Evaluation code 里粘贴或上传），Evaluate 和 Evolve 都用它打分；没有内置 metric，也没有“新建/选择 evaluator”。
- `EvaluatePanel.jsx`：写评测代码 → Check code 读参数 → 选已保存的 batch/run → Preview（不保存）/ Run（保存报告）→ 选指标 → 存为 workflow 的 evaluator（名称、时机、超时、标签），并管理已保存列表（编辑/运行/启停/删除）。
- `EvaluatorParams.jsx`：把 `/api/evaluators/interface` 返回的 params 渲染成表单（按类型、默认值、必填、报错行）。
- `draftStore.js`：粘贴的代码草稿在浏览器里的镜像；服务端 `evaluators/draft` 才是真值。
- `EvolveForm.jsx`：来源、Run/Batch、数据集、dev/test、模式及参数选择；目标 evaluator 来自 `graph.evaluators`。
- `EvolveResult.jsx`：轨迹评估指标、逐条结果、提示词对比和应用。
- `format.js`：显示格式与阶段名称。
- `EvaluationTab.jsx`：Batch 结果中的评分报告，可对这批结果运行某个已保存 evaluator；历史报告（包括已迁移掉的 evaluator 留下的）照样显示。
- `EvaluatorReports.jsx`：报告展示，batch/run 里保存的旧报告仍然读得出来。
- `BatchCompare.jsx`：两批结果对比。

数据接口在 `frontend/src/api.js`。后端在 `backend/features/evaluation`。
修改评分定义请同时检查后端，不能只改前端显示文字。
样式在 `frontend/src/styles.css` 的 evolve / evaluate 区域。

验证：`npm --prefix frontend test -- --run src/features/evaluation`。
