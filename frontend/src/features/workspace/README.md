# workspace 前端模块

- `WorkspacePanel.jsx`

共享依赖：`frontend/src/api.js`（HTTP）、`frontend/src/components`（公共控件）、`styles.css`（样式）。

验证：`npm --prefix frontend test -- --run src/features/workspace`，然后 `npm --prefix frontend run build`。测试与实现同目录。

Workspace 的 Upload files / Upload folder 共用目标目录（默认 `files`）；New file / New folder 也以该目录为默认路径。文件夹上传保留层级，逐个上传并显示进度，失败显示已完成数量。同名文件不覆盖。右键文件或文件夹 Copy path 复制服务端真实路径，复制后显示反馈；checkpoint 等二进制文件展示元信息，不开放文本编辑。上传的数据集挂载仍显示在 datasets 下。
