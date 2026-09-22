# Evaluation / Evolve 前端

- `EvolvePanel.jsx`：弹窗外壳、历史列表、轮询和页面切换。
- `EvolveForm.jsx`：来源、Run/Batch、数据集、dev/test、模式及参数选择。
- `EvolveResult.jsx`：轨迹评估指标、逐条结果、提示词对比和应用。
- `format.js`：显示格式与阶段名称。
- `EvaluationTab.jsx`：Batch 结果中的评分报告。
- `BatchCompare.jsx`：两批结果对比。

数据接口在 `frontend/src/api.js`。后端在 `backend/features/evaluation`。
修改评分定义请同时检查后端，不能只改前端显示文字。
样式仍在 `frontend/src/styles.css` 的 evolve 区域，Prompt 比较使用右侧主内容区单一滚动。

验证：`npm --prefix frontend test -- --run src/features/evaluation`。
