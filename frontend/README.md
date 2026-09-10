# Frontend maintenance

先读 [MAINTENANCE.md](../MAINTENANCE.md)。

- `src/features/`：按功能组织的组件、hooks 和同目录测试。
- `src/components/`：跨功能共享控件、顶部栏、公共菜单等。
- `src/api.js`：HTTP 客户端。供应商 LLM SDK 不进入前端。
- `src/App.jsx`：画布页面装配；`Platform.jsx`、`TaskDetail.jsx`：项目和任务页面。
- `src/styles.css`、`platform.css`：共享样式，保留原先覆盖顺序。

从仓库根目录运行 `npm --prefix frontend test -- --run` 和 `npm --prefix frontend run build`。
局部修改先执行 `npm --prefix frontend test -- --run src/features/<功能名>`。
