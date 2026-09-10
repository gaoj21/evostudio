# workspace 前端模块

- `WorkspacePanel.jsx`

共享依赖：`frontend/src/api.js`（HTTP）、`frontend/src/components`（公共控件）、`styles.css`（样式）。

验证：`npm --prefix frontend test -- --run src/features/workspace`，然后 `npm --prefix frontend run build`。测试与实现同目录。
