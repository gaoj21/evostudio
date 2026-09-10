# chat 前端模块

- `ChatActivity.jsx`
- `ChatComposer.jsx`
- `ChatPanel.jsx`
- `ChatSessions.jsx`
- `ResultChat.jsx`
- `chatSession.js`
- `useChatCancellation.js`

共享依赖：`frontend/src/api.js`（HTTP）、`frontend/src/components`（公共控件）、`styles.css`（样式）。

验证：`npm --prefix frontend test -- --run src/features/chat`，然后 `npm --prefix frontend run build`。测试与实现同目录。
