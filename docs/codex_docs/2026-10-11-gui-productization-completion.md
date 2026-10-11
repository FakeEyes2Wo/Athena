# Web GUI 产品化完成记录

日期：2026-10-11。用户要求先了解 Athena，再参照 Claude web 一类产品重做 Web GUI：
直观、美观、易用、交互流畅；做成面向用户的产品形态但不过度商业化；对阻碍工作的
前端（及相关实现）缺陷和不合理设计提出并修复。后端 RPC 协议
（`docs/athena-gui-design.md` §3）与 `usePipeline` 状态机契约保持不变。

## 完成内容

界面重做：

- 设计系统重写（`athena-gui/src/styles.css`）：暖中性浅/深色 token、CJK 字体回退、
  统一按钮/表单/徽章/卡片/表格/空状态/对话框样式、动效与 reduced-motion；新 logo
  与 favicon。浅色主题声明 `color-scheme: only light`，避免浏览器自动暗色覆盖用户
  明确选择的浅色；`index.html` 在首帧前解析主题，避免暗色用户看到闪白。
- 外壳：可折叠侧栏（新会话、六个视图、按工作区分组的会话，支持重命名/删除）、
  精简顶栏（运行状态、进度、暂停/继续/停止）、右侧「研究概览」、运行日志抽屉、
  后端断线横幅与不可达页面；工作区选择页重做。
- 对话：居中阅读栏、Markdown（含 GFM 表格）回复、可折叠工具块、贴底跟随；输入框
  自动增高、Enter/Shift+Enter、输入法安全、斜杠命令菜单，模式随会话状态切换
  （新任务/回答问题/修改任务理解/研究指导/已结束）；人工提问改为输入框上方的内联
  面板（倒计时、数字快捷键）；欢迎页含示例与就绪提示；任务理解卡片全面中文化。
- 视图页：研究树（实验谱系/假设图/图算法）、实验（筛选、搜索、详情含变更与产物、
  指标趋势）、数据分析、模型轨迹、报告（复制 Markdown、下载 .md）。设置改为分节
  对话框，并注明哪些设置仅作用于当前会话。

修复的阻塞缺陷与不合理设计：

- Gateway 每个连接严格串行处理请求：`task_clarification_start` 等待人工回答时，
  用户的 `human_reply` 被排在其后直到 120 s 超时。现按请求并发处理，会替换
  runtime 的方法仍串行（`src/gui_gateway/transport.py`）。
- 切回对话视图会重新 `session_switch`，重建 runtime 并取消待答的澄清问题；
  修改/重试后草稿停在 CLARIFYING 无人推进；草稿待确认时在输入框输入会触发
  `different_task`。
- 设置对话框深色模式白底、数据目录逐键 trim（无法输入含空格路径）、设置内直接
  改项目路径绕过工作区状态；停止研究与删除会话无确认；对话不跟随流式输出；
  EDA 内联图片被 Markdown 安全过滤丢弃。
- 模型轨迹整页不可用：`src/athena/gui/traces.py` 按虚构格式读取 `role`/`kind`，
  而 PydanticAI 实际写 `kind: request|response` 与 `part_kind`，导致所有消息被标为
  「输入」、工具调用变成空白条；另有 flex 收缩把 197 条消息压成细线。工具参数
  JSON 字符串现解析为对象，并显示命令/返回的一行预览。
- 长会话性能：3134 行转录一次性挂载约 4.1 万个 DOM 节点。现只渲染尾部窗口，
  「显示更早的消息」按需载入并保持阅读位置（约 2300 个节点）；修复普通消息因
  嵌套数组按位置取 key 导致窗口移动时整体重挂载。
- 贴底跟随只在消息变化时生效：内容异步增高或输入区变高后停在离底部约 60 px。
- 研究树/假设图初始缩放约 0.35，文字不可读且被小地图遮挡；现可读时整体适配，
  否则以 0.75 缩放对齐根节点。节点详情改到左上角，不再遮挡缩放控件。
- 后端真实存在的 `INCONCLUSIVE` 假设状态在前端无标签与颜色；假设图每条谱系边
  的「父→子」白底标签在深色下突兀（ReactFlow 样式后加载覆盖了主题）。
- Markdown 不支持 GFM 表格（报告与 EDA 中的指标表、混淆矩阵显示为原始 `|`），
  且表格窄列被逐字折行；指标趋势用平滑曲线暗示不存在的中间值；EDA/报告文档
  靠左留白。
- 任务理解失败时卡片列出全部「not supplied」英文字段，另有一条重复的英文错误
  气泡；`provider_failure` 现提示去「设置 → 模型连接」检查。示例提示填入后焦点
  仍在按钮上；设置分节导航点击后高亮跳到上一节；「门禁生成」等术语改为通俗说明。
- 开发与测试环境：Node ≥ 25 的全局 Web Storage 遮蔽 jsdom，71 个用例失败；
  Vitest 误收 `node:test` 启动器测试；Windows 保留 1399–1498 端口导致
  `npm run dev:web` 无法监听 1421，新增 `ATHENA_WEB_PORT`，启动器在前端失败时
  提示并退出而非遗留后端；gateway e2e 测试未隔离用户目录，会写入真实
  `~/.athena/gui_state.json`，隔离 fixture 已移到 `tests/conftest.py` 对全套生效。

## 验证证据

- 前端：`npm test`（athena-gui）23 个文件 239 passed（已变基到含 e580a14 的 main），`node --test scripts/dev.test.cjs`
  1 passed；`npm run build` 通过（主包 453 KB，gzip 144 KB）。
- Python GUI：`uv run pytest tests/test_gui_*.py tests/unit/gui -q` 91 passed，
  1 failed；失败为既有的 `test_gui_gateway_e2e.py::test_full_chat_flow`
  （`BaselineAuthorityError`，需要外部 baseline authority），与本次改动无关。
  新增回归测试：传输层阻塞请求不再饿死后续请求（旧代码失败、新代码通过）、
  PydanticAI 真实格式的轨迹投影、`ruff check` 通过。
- Python 全量（临时 HOME，`GIT_CONFIG_GLOBAL` 指向本机 git 配置）：2855 passed，
  1 skipped，42 failed。把同样 42 个用例放到 `git archive HEAD` 的干净副本上、
  以相同环境运行，42 个全部同样失败，属于本机既有/环境性失败（集中在
  baseline git 校验、PREPARE 评估隔离、断点恢复、工作区工具等），与本次改动无关。
- 隔离验证：以临时 HOME 运行 gateway e2e/handler 测试后，临时 HOME 下不再出现
  `.athena/`。此前测试在真实用户目录生成的 `gui_state.json` 已删除。
- 实机验证：在临时 HOME 与 TESS 演示项目副本上运行真实 gateway，通过浏览器逐页
  检查欢迎页、对话（3134 行转录）、研究树与节点详情、假设图、图算法、实验列表/
  详情/指标趋势、数据分析、模型轨迹、报告生成、设置各节、研究概览三个标签、
  运行日志、折叠侧栏、浅色/深色主题与 390/760 px 窄屏；无 API Key 时提交任务与
  重试均得到一致的失败卡片且不卡死。

## 边界

- 未配置模型密钥，未运行真实研究任务。
- 研究策略/算力设置保存在各会话的 `state.json`，假设生成方式、优化方向、验证
  策略、手动模式只存在于运行进程内，模型连接写入 gateway 工作目录下的 `.env`；
  新会话因此从默认值开始。这是后端设计问题，本次只在设置界面注明作用范围。
- 已完成运行的顶栏仍显示禁用的暂停/继续/停止（既有测试约定），未改动。
