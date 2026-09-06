# GitHub 交付说明

项目以原始 GitHub 仓库交付，不要求源码 ZIP。

提交时提供仓库链接和实际验收版本的完整 commit hash：

```bash
git rev-parse HEAD
```

主入口和 GUI 上手步骤以仓库根目录 README 为准。本目录是可选的
离线模型推理工具，不替代通过 GUI 运行 Athena 的流程。

`weights/`、`predictions/` 是本地生成物，不包含在 Git clone 中。
需要离线模型推理时，按本目录 README 的导出命令生成模型；若单独分发
已训练模型，须同时提供 metadata、checksums 和明确的来源说明。

此前生成的本地归档是历史辅助材料，不再是当前交付方式；无需为使用
GUI 下载该归档，也不应把它写成仓库安装的前置条件。

正式提交前补齐队伍联系人、数据来源/获取说明和响应窗口。历史 TESS
分数、新的 GUI 运行结果与离线预测一致性验证须分开描述；不宣称新做过
FINAL 评估或从未接触过 FINAL 的盲测历史。
