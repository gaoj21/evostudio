# 自定义输入数据集

`DatasetInput.jsx` 提供已保存数据集的选择、上传、预览、重命名、删除、字段映射和记录数量限制。
画布的 `../canvas/Inspector.jsx` 在 `user_dataset` 来源类型下装配此组件，并同步节点的 `source` 和 `outputs`。
HTTP 客户端在 `../../api.js`，后端实现在 `backend/features/data/user_datasets.py`。

测试：`npm --prefix frontend test -- src/features/data/DatasetInput.test.jsx`。
