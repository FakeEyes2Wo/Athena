# Rubric V2：GitHub Desktop 零基础协作指南

## 先理解四个词

- Repository（仓库）：项目文件加修改历史的集合。Athena 是一个 repository。
- Branch（分支）：从某个版本分出的独立工作线。你可以改功能而不直接影响团队的 `main`。
- Commit（提交）：给一组已检查的本地修改拍一个有说明的快照。
- Pull Request（PR）：请求团队把你的 branch 审查后合并进 `main`。

不要直接改 `main`。`main` 是团队共享稳定线；功能 branch 便于看 diff、跑测试、Review 和撤回。

## 推荐使用哪个交付物

如果团队最新 Athena 与本次原始 ZIP 完全相同，最简单的是使用 `Athena_Rubric_V2_complete.zip`。

如果团队在你开发期间继续改了 Athena，优先使用 `rubric_v2.patch` 或 `rubric_v2_changed_files.zip`，在最新代码的 feature branch 中应用，并逐个解决冲突。不要用完整 ZIP 覆盖团队最新仓库。

## 一、用 GitHub Desktop Clone 团队仓库

1. 打开 GitHub Desktop，登录团队 GitHub 账号。
2. 点顶部 `File` → `Clone repository...`。
3. 在 `GitHub.com` 标签里选择 Athena 仓库；找不到时点 `URL` 并粘贴仓库地址。
4. `Local path` 选择一个新文件夹。
5. 点 `Clone`。完成后左上角会显示 Athena 仓库，顶部 `Current branch` 通常为 `main`。
6. 点 `Fetch origin`，让 Desktop 获取远端最新状态。
7. 如果按钮变成 `Pull origin`，点它，把最新 `main` 拉到本机。

## 二、创建功能分支

1. 确认顶部 `Current branch` 是 `main`，并已完成 Pull。
2. 点 `Current branch` → `New branch`。
3. 输入：

```text
feature/rubric-evaluation
```

4. `Create branch based on...` 选择最新 `main`。
5. 点 `Create branch`。
6. 顶部应显示 `Current branch: feature/rubric-evaluation`。看到这个再复制/应用修改。

## 三、把 Codex 修改放到团队最新代码

### 方案 A：团队代码未变化，使用完整包

1. 解压 `Athena_Rubric_V2_complete.zip` 到临时目录。
2. 不要复制 `.git`、`.venv`、`.env` 或运行数据。
3. 把解压后的项目内容复制到 GitHub Desktop Clone 的 Athena 目录。
4. Windows 询问是否覆盖同名文件时，先确认当前 branch 正确，再允许覆盖。

### 方案 B：团队代码已有新修改，使用 patch（推荐）

1. 把 `rubric_v2.patch` 复制到 Clone 的 Athena 根目录。
2. 在该目录打开 PowerShell。
3. 先检查能否应用：

```powershell
git apply --check .\rubric_v2.patch
```

4. 若没有错误，再应用：

```powershell
git apply .\rubric_v2.patch
```

5. 若出现 `patch does not apply`，不要反复覆盖文件。请把错误和冲突文件交给熟悉 Git 的同事处理，或使用 changed-files 包逐文件合并。
6. `rubric_v2.patch` 是传输文件，不需要一起提交到产品仓库时可从 Clone 目录移走。

### 方案 C：使用 changed-files 包

1. 解压 `rubric_v2_changed_files.zip`。
2. 包内保持原目录结构。把内容复制到 Clone 的 Athena 根目录。
3. 对团队也修改过的文件，不要盲目覆盖；在编辑器中同时打开新旧版本，按功能合并。
4. 新文件可以直接复制；修改文件应重点检查 `runtime.py`、`agent_turn_runner.py`、`supervisor.py` 和 `prepare.py`。

## 四、查看 Changed Files

1. 回到 GitHub Desktop，左侧 `Changes` 会列出修改文件。
2. 点一个文件，右侧显示 diff。
3. 绿色带 `+` 的行表示新增；红色带 `-` 的行表示删除。修改通常显示为相邻的红/绿块。
4. 正常范围应与 `RUBRIC_V2_CHANGED_FILES.md` 一致：18 个 Production、6 个 Test、10 个 Docs/Delivery。
5. 若看到几百个无关文件、数据集、`.venv`、`.env`、缓存或整段文件全部红色，先停止 Commit。这常见于复制错目录、换行/编码变化或误删。
6. 判断是否误删大量代码：看左侧是否出现大量 `Deleted`，再点文件看右侧是否几乎全红。不要在没解释清楚前提交。
7. 点顶部 `Repository` → `Open in Command Prompt/PowerShell`，也可以运行：

```powershell
git status --short
git diff --stat
git diff
```

## 五、什么时候运行测试

应用修改并检查 diff 后、Commit 前先运行 targeted tests；再运行 integration 和 broader tests。Review 后继续改代码，也要重新运行受影响测试。具体命令见 `RUBRIC_V2_LOCAL_TEST_GUIDE.md`。

至少先执行：

```powershell
uv sync
uv run pytest -q test/unit/research/rubrics test/unit/research/supervisor/test_ideator_wiring.py test/integration/research/test_task_seeding.py
```

看到 `passed` 才表示这些测试通过。`failed` 或 `ERROR` 都不能当成通过。

## 六、Commit

1. 在 GitHub Desktop 左侧逐个勾选要提交的文件。
2. 不要勾选 `.env`、API Key、`.venv`、数据集、运行日志或临时文件。
3. 左下角 `Summary` 输入：

```text
Add Rubric V2 evaluation and hypothesis ranking
```

4. `Description` 可输入：

```text
Freeze an evaluation policy before PREPARE and rank post-Gate hypotheses with a validated LLM rubric plus deterministic fallback.
```

5. 再确认分支不是 `main`，点 `Commit to feature/rubric-evaluation`。

Commit 只保存在本机，尚未上传 GitHub。

## 七、Push / Publish Branch

第一次上传分支时，顶部会显示 `Publish branch`：

1. 点 `Publish branch`。
2. 等待完成，远端 GitHub 会出现同名分支。

以后新增 Commit 后，按钮通常显示 `Push origin`：

1. 点 `Push origin`。
2. 等待右上角进度结束。

Push 是把本地 Commit 上传到远端同一 branch，不等于合并进 `main`。

## 八、创建 Pull Request

1. Publish/Push 后，GitHub Desktop 通常显示 `Create Pull Request`，点击它；也可在浏览器打开仓库后点 `Compare & pull request`。
2. Base 选择 `main`，compare 选择 `feature/rubric-evaluation`。
3. Title 建议：

```text
Add Rubric V2 evaluation policy and hypothesis ranking
```

4. 把 `RUBRIC_V2_PR_TEMPLATE.md` 内容复制进描述，按团队实际情况补充。
5. NLP validation 与 Ablation 没有实际运行时，必须保持 `Pending local NLP validation` / `NOT RUN`，不要改成 Passed。
6. 添加 reviewer、关联 issue（如团队有），然后点 `Create pull request`。

## 九、Review 后继续修改

1. 在 GitHub Desktop 确认仍在 `feature/rubric-evaluation`。
2. 根据 Review 修改文件。
3. 重新检查 Changes 和运行受影响测试。
4. 新建一个说明清楚的 Commit，例如：

```text
Address Rubric V2 review feedback
```

5. 点 `Push origin`。现有 PR 会自动出现新 Commit，不需要新建 PR。
6. 在 PR 中逐条回复 reviewer，说明改动和测试结果；不要只写“已解决”。

## 十、负责人 Merge 后同步最新 main

1. 等负责人合并 PR。
2. GitHub Desktop 点 `Current branch` → `main`。
3. 点 `Fetch origin`，随后点 `Pull origin`。
4. 确认 `main` 已包含 Rubric V2。
5. 以后开发新功能时，再从最新 `main` 新建另一条 branch。
6. 已合并的 feature branch 可在确认无未推送 Commit 后删除；不确定时先保留。

## 最后检查清单

- 当前 branch 是 `feature/rubric-evaluation`，不是 `main`。
- Changes 数量与报告大致一致，没有 `.env`、Key、数据集或 `.venv`。
- targeted tests 已通过。
- PR 里真实写出 broader suite 的旧故障和未运行的 NLP/消融。
- Review 前没有自行 Merge。
