# library 前端模块

- `Palette.jsx`
- `ToolsPanel.jsx`

共享依赖：`frontend/src/api.js`（HTTP）、`frontend/src/components`（公共控件）、`styles.css`（样式）。

验证：`npm --prefix frontend test -- --run src/features/library`，然后 `npm --prefix frontend run build`。测试与实现同目录。
