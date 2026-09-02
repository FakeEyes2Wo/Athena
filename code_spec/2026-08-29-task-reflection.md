# 任务反思：前端越轨分析 → Spec → 简化修复

日期：2026-08-29
关联：`docs/前端分析.md`、`code_spec/2026-08-29-supervisor-owned-task-understanding-spec.md`

---

## 1. 反思框架

对这类“先分析、再定方案、再修复”的任务，使用四层反思：

1. **事实层**：我实际看到了什么代码路径？
2. **判断层**：我的结论是否由证据支持？
3. **边界层**：哪些条件没验证？哪些假设可能不成立？
4. **优化层**：哪些代码可以更简单、更一致、更少死代码？

---

## 2. 事实回顾

- GUI 新任务存在两条理解链路：`parse_intent` 和 Supervisor `maybe_run_task_understanding`。
- `parse_intent` 是后端 Python 服务执行的独立 LLM 澄清链路，不是浏览器直接推理。
- `parse_intent` 不写 `state.task_understanding`，因此后端 Supervisor 仍会再执行一次理解。
- 简单回填 `state.task_understanding` 后跳过 Supervisor 会丢失 `configure_kaggle` 决策。

---

## 3. 自我反思

### 3.1 做得对的

- 没有采用“用前端结果填充状态并跳过 Supervisor”的错误修复。
- 区分了“浏览器越权”和“后端存在两条理解链路”这两个不同表述。
- 最终选择了最简修复：删除 `parse_intent` 副链路，让 Supervisor 成为唯一理解入口。

### 3.2 之前判断需要修正的

- “前端理解结果没有回写后端”不够准确：理解结果其实是后端 `GuiService.parse_intent` 产生的。
- 正确表述应为：**后端存在一个独立于 Supervisor 的 `parse_intent` 入口，前端恰好选择并并行触发了它。**

### 3.3 风险与未验证项

- 删除了 `parse_intent` 后，原 `TASK_CLARIFICATION.md` 生成能力一并消失。
- 没有验证是否存在外部旧客户端仍依赖 `send_message` / `parse_intent` RPC。
- 前端仍保留基于本地投影的“新任务 / 运行中指导”路由，未完全按 spec 重构。
- Rust `cargo check` 因缺少 `resources/gui_gateway.exe` 无法完整验证，属于环境限制而非代码问题。

---

## 4. 反思后进一步优化

基于以上反思，追加了以下清理与收敛：

1. 前端测试中移除所有 `sendMessage` mock 和断言，避免死引用。
2. `TaskUnderstanding.needs_configuration` 改为可选，匹配后端 Supervisor 状态实际字段。
3. 在 Supervisor 工作流内补回最小 `TASK_CLARIFICATION.md` 生成：
   - 在 `ask_user` 外层记录澄清问答；
   - 任务理解完成后由后端写入 `handoff_refs["task_clarification"]`；
   - `agent_turn_runner` 重新读取该 handoff，供 Ideator 使用。
4. 新增后端单测，锁定“Supervisor 任务理解后生成唯一澄清 handoff”。

---

## 5. 优化后的状态

- 唯一理解入口：`start_search / start_task` → Supervisor。
- 前端只调用 `start_search`，预览由后端 `state.task_understanding` 填充。
- Rust / Python / TS 三层均移除 `parse_intent` / `send_message`。
- `TASK_CLARIFICATION.md` 由 Superisor 工作流生成，不再依赖独立副流程。
- `tsc --noEmit` 通过。
- Python 协议契约测试通过。
- 新增任务澄清单测通过。
