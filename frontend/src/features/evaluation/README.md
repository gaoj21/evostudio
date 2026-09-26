# Evaluation / Evolve 前端

评测不在画布上：画布没有 evaluator 节点，评测代码写在 Evaluation & Evolve 弹窗里。

- `EvolvePanel.jsx`：弹窗外壳、历史列表、轮询、和 New run。New run 有两种：Evaluation（在表单里粘贴/上传评估代码，对已保存结果或画布 Input 评估，代码随这次评估保存）和 Evolve（接着一次已完成的评估，用它的代码，选它报告里的一个指标来优化）。
- `EvaluationCode.jsx`：评估代码：粘贴/上传、从代码读参数、草稿保存、时间限制和标签。只在 New run → Evaluation 里用，不存到 workflow 上。
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
