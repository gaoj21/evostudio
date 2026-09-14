# canvas 前端模块

- `Inspector.jsx`
- `SourceNode.jsx`
- `TaskNode.jsx`
- `ToolNode.jsx`
- `canvasHandles.js`
- `convert.js`

共享依赖：`frontend/src/api.js`（HTTP）、`frontend/src/components`（公共控件）、`styles.css`（样式）。

验证：`npm --prefix frontend test -- --run src/features/canvas`，然后 `npm --prefix frontend run build`。测试与实现同目录。

## Workflow connections

`convert.js` matches exact field names and unambiguous case/separator variants.
Unmatched fields never implicitly create an order-only edge. `ConnectionEditor.jsx`
lets users map outputs to differently named inputs or explicitly choose ordering.
Click an existing workflow edge to edit its mapping or delete it. Inputs already
supplied by another data edge cannot be bound a second time. Saved mappings remain
explicit `{from, to}` pairs understood by the backend; existing order-only edges
are preserved until the user edits them.
