# execution 前端模块

- `RunDialog.jsx`
- `RunOutcome.jsx`
- `RunResultPage.jsx`
- `RunsPanel.jsx`
- `SchedulePanel.jsx`
- `WorkflowRunCard.jsx`
- `batchControl.js`
- `inputFiles.js`
- `runStates.js`
- `useExecutionSession.js`

共享依赖：`frontend/src/api.js`（HTTP）、`frontend/src/components`（公共控件）、`styles.css`（样式）。

验证：`npm --prefix frontend test -- --run src/features/execution`，然后 `npm --prefix frontend run build`。测试与实现同目录。
