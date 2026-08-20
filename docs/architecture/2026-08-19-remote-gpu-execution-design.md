# 远程 GPU 执行：需求分析与架构设计

Status: P0–P4 已实现（见第十一节）；P5 未做
Date: 2026-08-19
Base: main @ c31c8a7
Source of truth: `src/athena/execution/runtime.py`、`src/athena/research/supervisor/experiment.py`、
`src/athena/research/supervisor/scheduler.py`、`src/athena/cli.py`

需求：把若干 GPU 服务器或集群交给 Athena，让它通过 SSH 远程跑实验验证。

> 引用约定：文中出现的 `fork_project`、`research/fork.py`、`docs/survey_limits_ch.md`
> 目前都在 `feature/survey-overhaul` 分支上，**尚未并入 main**。引用它们是因为那批
> 工作恰好把本设计要面对的几个坑先踩过一遍，不代表 main 上已有这些代码。

本文回答三件事：这条缝该切在哪、切之前必须先补的洞是哪些、以及哪些代价必须
明写出来而不是等它在半夜静默发生。

---

## 零、先回答“要不要做”

只有**一台** GPU 服务器时，最完善的做法是不做本设计：`ssh gpu-01`，在那台机器上
直接跑 Athena。零代码，零新失败模式，评估器和数据天然同机。

值得建这套东西的判据只有一条：**需要多台机器同时推进同一棵研究树**。
Athena 的价值在于横向比较假设（同一个冻结评估器、同一个 baseline commit、
`fork_project` 分臂）。在 N 台机器上各跑一个 Athena，得到的是 N 棵互不相干的
树、N 个不同的评估器，比较无从谈起——这恰好是本仓库 `fork_project` 存在的理由。

所以本设计的前提是：**一个 Athena 进程（控制节点），N 台计算机器**。控制节点
可以是笔记本，也可以是其中一台 GPU 机。

### 零点五、本次锁定的配置

| 维度 | 选定 | 它强制了什么 |
|---|---|---|
| 资源形态 | **裸 SSH 机器** | Athena 自己当调度器：`nvidia-smi` 探测、卡的分配、`CUDA_VISIBLE_DEVICES` 强制、租约持久化。见 §4.3 `BareLauncher`、§九·1 |
| 控制节点 | **当前这台 Windows** | 跨平台不对称最严重的一种。见 §4.7 |
| 数据集 | **大，且无共享存储** | 分发本身是独立一期，并且会反过来改写放置策略。见 §4.8 |

这三条凑在一起是**最难的一个组合**：没有调度器替你管卡，没有同构环境替你抹平
路径，没有共享存储替你省掉分发。好处是设计里任何一处偷懒都会立刻暴露，而不是
等换台机器才发现。

后文的 `SlurmLauncher` / `DockerLauncher` 与共享存储相关的段落保留，但**本次不做**，
只作为接口不被设计死的证据。

---

## 一、现状：一条缝，两条执行路径

### 1.1 唯一的 agent 执行缝

`ExecutionRuntime`（`execution/runtime.py:648`）是 agent 侧唯一的命令执行门面：

- `shell_command` 工具（agent 探索、调试）→ `ExecutionRuntime.run(command=...)`
- experiment manifest 的 argv 命令（确定性实验）→ `ExecutionRuntime.run(argv=...)`
  （`supervisor/experiment.py:336`）

全仓库只有一处构造它（`research/runtime.py:189`）。它的类 docstring 里已经写着
`TODO(remote-executor)` 与 `TODO(container-executor)`——这条缝是设计时就留好的。

### 1.2 第二条执行路径，而且必须留在本地

`DataScriptRunner`（`research/script_runner.py`）**绕过** `ExecutionRuntime`，
直接 `subprocess.run` 跑 `uv` 与冻结的评估 bundle。`TrustedEvaluator` 用它打分。

这不是遗漏，是本设计里最重要的一条边界：

> **评估器永不出门。** 测试标签、冻结 bundle、打分过程全部留在控制节点。
> 远端只产出 `predictions/`，回到本地才被打分。

理由与仓库里已经付过学费的那件事同源：一旦训练环境能碰到标签，泄漏就会以
“这是标准做法”的形态混进来（见 `docs/survey_limits_ch.md` 第六节，PREPARE
第二次跑出 AP 0.4110 > 贝叶斯上界 0.3682 的那次）。物理隔离是唯一可靠的堵法，
而现在的代码恰好已经是分开的——**别在远程化的时候把它合上**。

### 1.3 调度单元

`Scheduler.next_actions` 按 `state.concurrency` 填槽（`scheduler.py:129`，默认 1）。
每个槽 = 一个 git worktree（`workspaces/` 下）+ 一个 PlanAgent。

**这就是租约的天然粒度：1 个 Plan 槽 ↔ 1 份 GPU 租约。**

---

## 二、必须先修的八件事，没有一件在 SSH 上

这一节是本次研究的主要产出。下面每一条，在接上 SSH 之前修不掉，远程执行就
只是把失败搬到了另一台机器上。

| # | 阻塞 | 位置 | 后果 |
|---|------|------|------|
| 1 | manifest 命令硬编码 120 s 超时 | `experiment.py:299` `timeout_s: int = 120`；两处构造（`phase_runner.py:47`、`prepare.py:257`）都不覆盖 | 值得派去 GPU 集群的作业没有一个能在 120 秒跑完。不改这条，远程实验 100% 返回 `error="timeout"` |
| 2 | 数据集只是 prompt 里的一句绝对路径 | `cli.py:44` 的 `Dataset path: {args.data}` | agent 写出的脚本里是宿主机绝对路径。换到 Linux GPU 机，第一行 `read_csv` 就炸。`DataCard.dataset_ref` 存在但只喂 Rust 契约固件，不在活路径上 |
| 3 | 环境描述来自本地 `os.name` | `runtime.py:189/193/316` | 远端是 Linux 时，模型仍被告知 `OS: Windows` / `Shell: powershell.exe` / 用 `;` 而非 `&&`，于是给 bash 写 PowerShell |
| 4 | `build_env` 转发本地路径变量 | `runtime.py:209`，`_HOST_VARS` 含 `PATH`/`HOME`/`TEMP`/`SSL_CERT_FILE`/`UV_CACHE_DIR` | 这些值在远端全是无效路径；`SSL_CERT_FILE` 还会让远端 TLS 直接失败 |
| 5 | 进程树终止用本地 `taskkill`/`killpg` | `runtime.py:470` | 远端不适用。断线留下占着几十 GB 显存的孤儿训练进程，是这类系统第一位的运维故障 |
| 6 | 输出解码回退用本地代码页 | `runtime.py:371` 的 `locale.getpreferredencoding()` | 远程 Linux 是 UTF-8，用本地 GBK 回退制造 mojibake |
| 7 | 环境变更无锁 | `runtime.py:655` `TODO(environment-lock)` | `concurrency > 1` 时多个臂并发 `uv add` 同一个 env root。远程化会把并发默认值推上去，这条从“理论问题”变成常态 |
| 8 | 全仓库没有 GPU 概念 | 在 `src` 下检索 gpu/cuda/nvidia：0 命中 | 无法表达“这个实验要 1 张卡 / 40 GB 显存”，也无法把硬件写进证据 |

第 1、2 条即使**永远不做远程**也该修：前者让长实验能跑完，后者让项目可搬迁。
建议单独成一期先落地。

---

## 三、切在哪：三种切法

**A. 给 agent 一个 ssh 工具（或放任它用 `shell_command` 自己 ssh）。**
最省事，也最不该做。取消、计费、复现全部失守；manifest 的 `outputs.predictions`
会落在错误的机器上；模型有能力 ssh 到持有标签的机器。**否决。**

**B. 只把 manifest 的 argv 远程化，`shell_command` 留在本地。**
诱人：manifest 是声明式、argv-only、输出已声明的可复现单元。但 agent 的循环是
“写脚本 → 跑 → 读报错 → 改”。如果 `shell_command` 在本地 CPU、manifest 在远程 GPU，
agent 会先用 `shell_command` 试跑、看到 `CUDA not available`，然后写出 CPU 代码。
**否决——调试回路和执行回路必须在同一台机器。**

**C. 在 `ExecutionRuntime` 之下换后端，粒度是 Plan。（推荐）**
一个 Plan 拿到租约后，它的**全部**命令——`shell_command` 与 manifest——都落在
同一台机器、同一个目录。上层（Supervisor、Scheduler、评估器、git）完全不知道
远程的存在。

这就是“优雅”在这里的定义：**远程性在执行缝之上完全不可见，在缝之下完全彻底。**

---

## 四、推荐架构

### 4.1 分层

```
GpuPool / Placement          决定去哪台、用哪几张卡、公平性、健康
   |   Lease(plan_id) -> Target(host, workdir, gpu_ids, launcher)
   v
ExecutionRuntime             门面不变；按租约选后端
   |-- LocalBackend          今天的 CommandExecutor + EnvironmentManager，行为不变
   `-- SshBackend            每份租约一条常驻通道
          `-- 远端 supervisor  一次性上传的自包含脚本：spawn / stream / cancel / put / get
```

`EnvironmentManager` 需要一分为二：

- `EnvironmentSpec`（声明式，两端共享）：uv 项目哈希、python 版本。
  `environment_hash()`（`runtime.py:244`）已经是现成的缓存键。
- `HostEnvironment`（后端提供）：shell、PATH、env 变量、工具版本、`runtime_summary`。

第 3、4、6 条阻塞是同一个根因：`EnvironmentManager` 现在既**描述**环境又**构造**
环境，且两者都写死在本地。拆开就一并解决。

> **后来的收窄（2026-08-20，见 11.5）**：远端只保留「描述」，不做「构造」。
> GPU 机上的解释器**现成什么样就什么样**，Athena 不在那边建环境根、不装依赖。
> 于是 `EnvironmentSpec` 是纯本地概念，远端那半只剩 `HostEnvironment`。

### 4.2 租约：粒度是 Plan

`ExecutionContext` 已经带 `experiment_id`（`runtime.py:96`），就是租约键。

- 生命周期 = Plan 的生命周期。Plan 结算或放弃即归还。
- 持久化：Athena 重启后租约不能凭空消失，否则显存被孤儿占着而池子以为它空闲。
- 排队可见：拿不到租约时 Plan 进入显式 `QUEUED` 状态，**不静默退回本地 CPU**。

### 4.3 三个 Launcher 覆盖裸机 / 集群 / 容器

后端只管“把 argv 送到远端并流式返回”，**怎么申请算力**交给 Launcher：

| Launcher | 包装 | 取消 | 适用 |
|---|---|---|---|
| `BareLauncher` | `CUDA_VISIBLE_DEVICES=<ids>` + 新进程组起 argv | kill 进程组 | 裸 SSH 机器，Athena 自己当调度器 |
| `SlurmLauncher` | `srun --gres=gpu:1 --time=...` 包住 argv | `scancel <jobid>` | 已有调度器的集群；Athena **不得**自己分配卡 |
| `DockerLauncher` | `docker run --gpus device=<ids>` | `docker kill` | 需要隔离时；顺带关掉 `TODO(container-executor)` |

集群与裸机的关键差异必须显式建模：

- **裸机**：Athena 是调度器，要自己探测（`nvidia-smi --query-gpu=index,name,memory.total,memory.used --format=csv`）、分配，并用 `CUDA_VISIBLE_DEVICES` 强制。
- **Slurm**：调度器已经存在，Athena 只提交并等待。SSH 连的是登录节点，工作区落在
  共享存储上（NFS/Lustre，反而省掉镜像）。**排队等待时间不可预测**，
  `ExecutionMonitor` 的 STALLED 判据必须把排队与“跑起来但不吐字了”区分开，
  否则每次排队都被判成卡死。

### 4.4 为什么用系统 `ssh` 而不是 Python SSH 库

推荐直接调用 OpenSSH 客户端，Athena 侧不新增依赖：

- `~/.ssh/config` 全部生效：`ProxyJump`（集群堡垒机的常态）、`IdentityFile`、
  端口、`known_hosts`、硬件密钥 / ssh-agent。
- 配置里只写一个 **Host 别名**，不写用户名、IP、端口、密钥路径。Athena 的配置
  文件里永远不出现凭据——与仓库现有的密钥卫生一致（密钥进 `.env` 或 `~/.ssh`，
  绝不进 `config.toml`）。
- 仓库已有 shell-out 的既有做法（`shutil.which` + `subprocess`），不引入新的
  CVE 面。

代价，明写：Win32-OpenSSH **不支持 ControlMaster 连接复用**。若按“每条命令一次
`ssh`”实现，Windows 控制节点上每条命令都要付一次完整握手。这正是下一条设计选择
的理由。

### 4.5 每份租约一条常驻通道

不要“每条命令起一个 `ssh`”。起**一条**长连接 `ssh`，远端跑一个几十行的自包含
supervisor 脚本，双方用一行一条 JSON 的小协议对话：

```
-> {"op":"spawn","argv":[...],"cwd":"...","env":{...},"id":"c17"}
<- {"op":"stdout","id":"c17","data":"..."}
<- {"op":"exit","id":"c17","code":0}
-> {"op":"cancel","id":"c17"}
```

一次设计同时解决四件事：

1. **延迟**：握手一次，摊到整个 Plan 的几十上百条命令上；ControlMaster 的缺席
   变得无关紧要，跨平台一致。
2. **取消**：`cancel` 消息 → 远端杀自己 `setsid` 出来的进程组。补上第 5 条阻塞。
3. **孤儿**：supervisor 的 stdin 就是 SSH 通道。连接一断即 EOF，supervisor 醒来
   杀掉进程组再退出。**这是显存不被永久占住的唯一可靠机制。**
4. **传输**：`put`/`get` 走同一条通道，不需要另开 scp/rsync（Windows 无 rsync）。

### 4.6 工作区：本文最难的一个取舍

本地 worktree 是 git 提交的依据（`LocalGitWorkspace.commit` 在本地算 diff），而
`read_file`/`write_file` 也绑死在本地目录（`agents/tools/generic_tools.py:40` 的 `generic_tool_registry`）。
命令一旦远程执行，两边就会分叉。两条路：

**（甲）镜像（推荐作 v1）**：本地为准。每条命令前推增量，命令后拉回
`manifest.outputs.*` 加上一份源码白名单（文本/代码，带体积上限）。

- 优点：git 提交、`read_file`/`write_file`、agent 的心智模型全部不变，改动面最小。
- 代价：checkpoint、特征缓存这类大文件**留在远端不回来**。必须在证据里显式
  记下“哪些路径只存在于远端”，否则 agent 会以为文件丢了并重跑。

**（乙）远端为家（更干净的终态）**：工作区就在远端，`read_file`/`write_file`
也走通道，本地只留 git 仓库，每个 turn 拉一次源码增量做提交。

- 优点：没有分叉，没有镜像策略，没有“只存在于远端”的路径。
- 代价：要动 `LocalGitWorkspace` 与文件工具；每次 `read_file` 多一个 RTT。

镜像时有一个具体的坑，仓库里刚踩过：worktree 根的 `.git` 是一个指回**源仓库**
的纯文本指针（见 `research/fork.py` 的 `WORKTREE_LINK`，`047f379`）。原样推到
远端，那里的 git 会去解析一条不存在的宿主机路径。推之前必须剔除。

### 4.7 Windows 控制节点到 Linux GPU 机：不对称清单

这一节每一条都是**静默或半静默**失败——接线时不报错，第三个实验的第七轮才炸。

| 不对称 | 表现 | 处理 |
|---|---|---|
| 路径分隔符与盘符 | 本地是 `WindowsPath`，远端是 POSIX；`ExecutionContext` 三个根都是 `Path` | 跨界只传**相对 posix 串**，绝对路径由各侧后端自己拼。manifest 已强制相对路径（`experiment.py` 的 `_validate_relative_path`），这条现成 |
| **行尾** | Windows git 若 `core.autocrlf=true`，签出的脚本带 CRLF；Linux 上 shebang 行末尾多一个 CR，直接报 `bad interpreter`；Python 脚本则是每行尾部多一个不可见字符 | `LocalGitWorkspace.init` 建仓后显式设 `core.autocrlf=false`（或写一份 `.gitattributes`）；传输时不做任何行尾转换 |
| shell 与环境描述 | `runtime_summary` 告诉模型 powershell，实际跑的是 bash | 阻塞 #3；`HostEnvironment` 必须出自后端 |
| 可执行位 | Windows 侧没有 x 位，传过去脚本不可执行 | manifest argv 本来就是 `["python","train.py"]` 而非 `./train.py`；**保持这个约定，别放宽** |
| 输出编码 | `_stream_fallback_encoding` 用本地 GBK 回退 | 阻塞 #6；回退编码由后端给，SSH 后端固定 UTF-8 |
| 无 ControlMaster | Win32-OpenSSH 不支持多路复用 | §4.5 的常驻通道从「优化」变成**必需** |
| 无 rsync | Windows 无 rsync | 增量走 §4.5 的通道自己实现；一次性大文件可用内置 `scp`，但别拿它做增量 |
| 大小写敏感 | `Train.csv` 与 `train.csv` 在 Windows 是同一个文件，在 Linux 不是 | 传输后做清单校验：文件数与逐个哈希都要对上 |
| 时钟与 mtime | 增量若靠 mtime，跨时区/跨文件系统不可靠 | 增量判据用 `(相对路径, size, sha256)` 清单差集，**不用 mtime** |

### 4.8 大数据集、无共享存储：分发是独立的一期

三个答案里成本最高的一个，而且它会反过来改调度。

**1. 优先让远端自己去取，而不是从 Windows 上推。** 数据若有上游来源（Kaggle
比赛、一个 URL、对象存储），让 GPU 机直接拉。理由很实际：GPU 机的出口带宽通常
比一台办公 Windows 机高一个数量级，且推送期间控制节点不能休眠、不能断网。仓库
已有 Kaggle stack，这条路是通的。**只有在确实没有上游可拉时才走本地推送。**

**2. 内容寻址 + 完成标记 + 断点续传。** 落在 `<scratch>/data/<清单 sha256>/`，
全部文件校验通过后才写 `.complete`；没有这个标记一律当作不存在。

> 半个数据集是本设计里最阴的失败：脚本照跑、分数照出、只是少了一半样本，
> 没有任何一层会报错。这与 `corpus_ref` 两臂都是 `None` 是同一类事故。

**3. 一台机器一份，不是一个 Plan 一份。** 数据是不可变共享物；租约只 pin 它，不拥有它。

**4. LRU 回收 + 租约 pin。** scratch 会满；被任一活跃租约引用的数据集不得回收。

**5. 放置必须带数据亲和。** 这条改写 §4.2 的调度：一个 Plan 落到没有数据的机器上，
要先付一次完整分发。所以顺序是 **已有数据的空闲机 > 空闲机 > 排队**，
而不是简单的 pack/spread。

但它会与 §五·6 的 `homogeneous` 约束打架——同型号的机器可能恰好都没数据。
**冲突时同构优先**：分发只是慢，异构是结果不可比。

**6. `ATHENA_DATA_ROOT` 从「该做」升级为「先决」。** 阻塞 #2 在本配置下是硬阻塞：
远端数据路径是 `<scratch>/data/<hash>/`，agent 既猜不到也不该知道。

---

## 五、必须成立的六条不变式

设计是否“完善”，就看这六条守不守得住。每条后面是今天违反它的位置。

1. **一个 Plan 的所有命令落在同一台机器同一个目录。**（今天：无租约概念）
2. **模型看到的环境描述来自真正执行命令的那台机器。**（今天：`runtime.py:316` 读本地 `os.name`）
3. **评估器与标签永不出门。**（今天：已经满足——`DataScriptRunner` 与 `ExecutionRuntime` 本就分离。别改坏）
4. **通道断开必然杀死远端进程组。**（今天：`runtime.py:470` 只会本地 `taskkill`）
5. **绝不静默降级。** 拿不到 GPU 就排队或明确失败，不偷偷用本地 CPU 跑完再报一个分数。
6. **硬件写进证据。** `PlanRunner` 写 evidence 时加一个 `placement` 块：host、
   GPU 型号、卡数、驱动/CUDA 版本、排队与传输耗时。

第 5、6 条不是运维洁癖，是**实验有效性**问题。Athena 的产出是假设之间的比较；
A 臂拿到 A100、B 臂拿到 3090，在墙钟受限的实验里两者的分数不可比。异构池必须
支持一条放置约束：**同一次比较（同一 baseline fork 出来的各臂）必须落在同型号
硬件上**。这与冻结评估器是同一个道理——把变量按住，只留一个。

---

## 六、配置面

```toml
[compute]
mode = "local"        # local | ssh | slurm
placement = "pack"    # pack | spread | homogeneous（同组同型号）
fallback = "never"    # never | ask —— 不提供 "silent"

[[compute.hosts]]
name    = "gpu-01"
ssh     = "gpu01.lab"        # ~/.ssh/config 里的 Host 别名，不是 user@ip
scratch = "/scratch/athena"
gpus    = "auto"             # auto = nvidia-smi 探测；也可写 [0,1,2,3]
max_leases = 4

[compute.slurm]
partition = "gpu"
gres = "gpu:1"
time = "04:00:00"
```

`config.toml` 的优先级链（系统环境变量 > `.env` > `config.toml` > 默认值）已经
存在，直接沿用。**连接凭据一律不进 Athena 的任何配置文件**，全部交给 `~/.ssh`。

### 注册时预检，而不是实验时才发现

每台机器登记时跑一次探针，把结果存成 host card：可达性、python 版本与已装的包、
`nvidia-smi` 与驱动/CUDA 版本、scratch 剩余空间、时钟偏移、能否 `setsid`。
任何一项不过就在注册时红掉。这类系统最典型的浪费是：跑了两小时的 PREPARE，
在第一个实验才发现远端跑不了。

**这件事同时要能单独跑**：`Athena-cli compute --check`（见 11.5）。验一台机器
不该需要起一整轮研究。它连上每台机器，把 GPU、解释器、已装的包、磁盘余量，
以及**会被原样注入给 agent 的那段 Runtime 原文**打出来——最后一项是重点，
因为它是最容易悄悄说谎的一处，而它说谎的代价是 agent 按错的前提写一整轮代码。

---

## 七、可观测与计费

每份租约记录：排队等待、上行字节/耗时、远端忙时、下行字节/耗时、
`nvidia-smi` 采样的利用率与显存峰值。

`ExecutionMonitor`（`execution/monitor.py`）已经是“source-independent”的、带
STALLED/TIMEOUT 推导，但目前只接在 `app_server/observability.py`，研究链路没用。
远程作业正是它的目标场景：训练循环不再吐字 = STALLED，而这在本地短命令场景下
没什么价值，所以一直没接。

耗时口径沿用 `docs/survey_limits_ch.md` 第八节的教训：**忙时（可重叠）与墙钟
（阶段划分）不能相加**。多机之后忙时之和会轻易超过墙钟，报表必须分开列。

---

## 八、安全

远程执行意味着 **LLM 写的代码以真实用户身份跑在你的 GPU 服务器上**。这比“跑在
我自己笔记本上”是一次实质升级，必须明说。

- 专用非特权用户；每份租约一个 scratch 子目录；不共享 home。
- `ForwardAgent no`；不向远端转发任何本地密钥类环境变量。
  `_HOST_VARS`（`runtime.py:32`）对远端应收缩到近乎只剩 `LANG`/`LC_*`。
- 远端 supervisor 脚本按哈希校验，每份租约上传一次。
- 可选 `Match User athena` + `ForceCommand`，让该账号除了 supervisor 什么都跑不了。
- **这不是沙箱。** 需要真隔离就走 `DockerLauncher`。`docker/experiment.Dockerfile`
  可以作起点，但它是 `python:3.11-slim`、CPU-only，要换 CUDA 基础镜像。

---

## 九、限制（明写，不藏）

1. **共享裸机上 Athena 无法保证独占。** 别的用户随时能在“你的”卡上起进程。
   缓解：主机侧协作式租约文件（如 `/var/tmp/athena-leases/`）；兜底：把观测到的
   争用写进证据，让不可比的比较**可被发现**，而不是假装没发生。
2. （本次不适用，接 Slurm 时再看）**Slurm 排队时间不可预测**，会主导墙钟；
   `--time` 估不准就要么被杀要么排不上。
3. **镜像方案下大文件留在远端**，agent 的“文件不见了”体验必须靠证据里的
   remote-only 路径清单来补。
4. **异构池 + 墙钟受限实验 = 混淆**。见第五节第 6 条；只有 `homogeneous` 放置
   策略能消除，代价是并行度下降。
5. **控制节点是单点。** 它挂了，所有租约靠远端 supervisor 的 EOF 自清理；这条
   路径必须被真正测过（拔网线级别的测试），不能只在设计里写着。
6. **多个 Athena 进程共用一个池**时，租约需要跨进程互斥。首版建议限定单控制
   节点，并在池子里记录 owner pid，发现第二个就直接报错，而不是默默重复分配。
7. **控制节点是 Windows 笔记本，就多一类可用性问题**：休眠、Wi-Fi 漫游、
   系统更新重启，都会切断所有常驻通道。远端 supervisor 的 EOF 自清理因此不是
   边缘路径而是**日常路径**，必须按日常路径来测。
8. **首轮数据分发可能以小时计**，且期间控制节点不能睡。这是选择「远端直取」
   而非「本地推送」的首要理由；没有上游可拉时，这段时间必须显式可见，
   不能表现为「Athena 卡住了」。
9. **裸机上的显存超卖无法阻止。** 租约只记账，不隔离；两个租约各以为独占一张卡
   时不会有任何报错，只会 OOM 或互相拖慢。`DockerLauncher` 是唯一的真解，本次不做。

---

## 十、分期与验收（按本次锁定的配置）

**P0 — 不依赖远程，先做。**
- 解除 manifest 的 120 s 硬编码超时并可配置（阻塞 #1）
- `ATHENA_DATA_ROOT`：`--data` 不再以原文进 prompt，改为注入环境变量，
  prompt 只说「数据在 `$ATHENA_DATA_ROOT` 下」（阻塞 #2）
- `.athena/repo` 显式 `core.autocrlf=false`（§4.7 行尾）

> 这条已核实、不是假想：本机 `git config core.autocrlf` = `true`；Athena 源码仓
> 靠 `.gitattributes` 的 `* text=auto eol=lf` 挡住了，但 `LocalGitWorkspace.init`
> 只跑一句 `git init -b main`（`core/git_workspace.py:78`），既没有 `.gitattributes`
> 也没设 `autocrlf`，于是**项目仓完整继承 `true`**。今天全在 Windows 上跑，无害；
> 文件一旦送到 Linux GPU 机就开始咬人。

验收：本地跑通一个超过 120 s 的实验；把项目连同数据整体换个盘符，重跑不用改
任何 agent 生成的代码。

**P1 — 后端抽象，行为一字不变。**
- 抽出 `ExecutionBackend` 协议，`LocalBackend` 包住今天的 `CommandExecutor`
- `EnvironmentManager` 拆成 `EnvironmentSpec` + `HostEnvironment`（阻塞 #3/#4/#6 一并解决）

验收：`test/unit/execution/` 全部原样通过，一行不改。

**P2 — 单机 SSH 打通（先不分配卡、不分发数据）。**
- 常驻通道 + 远端 supervisor（阻塞 #5）
- 工作区镜像（§4.6 甲案），清单用 `(相对路径, size, sha256)`
- 数据先手工放到远端，`ATHENA_DATA_ROOT` 手工指过去

验收：同一个 manifest 本地与远程各跑一次，分数一致；主动切断通道后，远端
`nvidia-smi` 里那个进程在 N 秒内消失。

**P3 — 数据分发。**
- 远端直取优先；内容寻址 + `.complete` + 断点续传 + 清单校验
- LRU 回收 + 租约 pin

验收：分发中途 kill 掉，重跑必须**续传**而不是重来；人为删掉远端一个文件，
下次使用必须**报错**而不是照常跑。

**P4 — 池与租约。**
- `nvidia-smi` 探测、host card 注册预检、租约持久化、`CUDA_VISIBLE_DEVICES` 强制
- 数据亲和 + `homogeneous` 放置（冲突时同构优先）
- evidence 里的 `placement` 块（阻塞 #8）

验收：见下方四条反向断言。

**P5 — 本次不做，只保证接口不被设计死。** `SlurmLauncher`、`DockerLauncher`
（后者顺带关掉 `TODO(container-executor)`）。

### 验收测试必须能抓住“静默没走远程”

这是本仓库付过学费的失败模式：`corpus_ref` 两臂都是 `None`，整条链路全绿，
测出来的东西却与调研毫无关系（`docs/survey_limits_ch.md` 第三节）。同样的洞在
这里长这样：SSH 连不上 → 悄悄本地跑 → 分数照出、日志照有、没人发现。

所以验收判据不能是“跑通了”，必须是**反过来会红**的断言：

1. 实验证据里 `placement.host != "local"` 且 `placement.gpu.name` 非空；
2. 同一个 manifest 在 `--compute local` 下跑，耗时显著更长（GPU 确实在用）；
3. 主动切断 SSH 通道后，远端 `nvidia-smi` 里那个进程在 N 秒内消失；
4. 把 `[compute]` 配置里的 host 改成一个不存在的别名，运行必须**失败**，
   而不是回落到本地并给出一个分数；
5. 删掉远端数据集里的一个文件（或它的 `.complete` 标记），下一个用到它的实验
   必须**报错**，而不是用少一半的数据跑出一个分数。

第 4、5 条是其中最重要的两条：它们测的不是功能，是第五节的**不静默降级**，
以及 §4.8 里那个「半个数据集」的坑。

---

## 十一、实现记录（2026-08-19）

P0–P4 已实现并提交在 `compute-resources` 分支。P5（Slurm / Docker）按第十节的
计划**未做**，接口留着。

| 期 | 提交 | 落点 |
|---|---|---|
| P0 | `b750452` | manifest 超时可配、`ATHENA_DATA_ROOT`、项目仓 `autocrlf=false` |
| P1 | `b6dc75c` | `execution/backend.py`：`ExecutionBackend` + `LocalBackend` |
| P2 上 | `34144eb` | `execution/remote/`：agent、channel、ssh、mirror |
| P2 下 + P4 | `d9e18cc` | `execution/pool.py`、`compute_config.py`、租约接进研究循环、证据里的 placement |
| P3 | `982e9b2` | `execution/remote/dataset.py`：内容寻址分发与数据亲和 |

### 11.1 设计里被实现推翻的三处

**（一）"Windows 上没有 setsid 的等价物，所以远端只能是 POSIX"——过强。**
`CREATE_NEW_PROCESS_GROUP` + `taskkill /T` 就是等价物，本地执行器一直这么用。
两边都支持之后，整条协议才能在开发机上跑**真验证**：真起进程、真杀进程组、
真读回字节，只剩 ssh 那一跳没被覆盖。这个改动的收益远大于"少写一个分支"。

**（二）`ssh host python3 -` 把源码从 stdin 喂进去——不成立。**
解释器会把整个 stdin 当程序读完，stdin 也就没了；而 stdin 正是协议通道本身，
更是"断线即 EOF、远端自己清场"这条机制的全部依据。改成 `-c` 加 base64 源码，
顺带穿过 ssh 那层 shell 不需要任何转义技巧。

> **这一条后来在真机上又被推翻了一半**：命令行装不下 13 KiB 的源码（见 11.4）。
> 最终形态是两级——`-c` 里只放一个 76 字节的加载器，源码作为 stdin 的头一段、
> 带长度前缀送过去。"stdin 不能被解释器当程序读掉"这个约束仍然成立，只是靠
> "读满 N 字节就停手"来满足，而不是靠不碰 stdin。

**（三）`SshBackend` 不实现 `ExecutionBackend`，少一个 `collect_outputs`。**
这是有意留的类型缺口。远程执行绕不开"文件怎么在两台机器之间搬"，给它补一个
空实现会让"产物没拉回来"变成一次静默的空目录；缺着，它就只能是一个类型错误。
完整后端是 `MirroredBackend(SshBackend, mirror)`。

### 11.2 实现过程中查出的、原本不在清单上的缺陷

**`runtime_summary` 教给 agent 的环境变量写法在 Windows 上是错的。**
注入子进程的是环境变量，而 PowerShell 里 `$FOO` 取的是 PowerShell 变量，未定义
就静默展开成空串。实测 `echo "[$ATHENA_ENV_ROOT]"` 得到 `[]`，`$env:` 写法才拿
到真路径——也就是说这台机器上每一句 `uv add --project "$ATHENA_ENV_ROOT"` 实际
都是 `--project ""`。这是第二节阻塞 #3 的一个具体实例，已随 P0 修掉。

**流式根本没在流。** 远端 agent 原本用 `BufferedReader.read(n)`，它会一直等到凑
满 n 字节或 EOF——一条几十字节的训练日志要等进程结束才出现。改 `read1` 之后，
同一组用例从 64 秒降到 6.6 秒。

**asyncio 的 `StreamReader` 默认只给 64 KiB 一行**，而一块 64 KiB 输出经 base64
是约 88 KiB，正好越界，表现成通道"莫名其妙断开"——排查方向会完全跑偏。

**租约原本只在 runtime 关闭时归还。** 每个跑完的 Plan 都还占着一张卡，池子会在
第 N 个实验上无谓地耗尽，而表现是"排队排不到"。已改为结算即归还。

### 11.3 自己写的两个用例是空的

抽查"回退修复看它红不红"时发现：

- "不校验就盖完成章"——照样全绿。顺利路径上校验本来就是空操作。
- "去掉数据亲和"——照样全绿。两台机器负载相同时排序退化到按名字，本来就会选中
  同一台。

两条都补强之后才真的变红。这与六点九那次是同一个教训：**为"这类失败很安静"而
写的测试，本身可以同样安静地失效。**

### 11.4 真机验收（2026-08-20）

在一台真的 GPU 机上跑通了：Windows 控制节点 → SSH → Linux 容器，
1×NVIDIA vGPU-32GB，Python 3.12.3（conda），无 uv。第十节末尾那四条反向断言
全部拿到，端到端一次实验 6 秒。

| 断言 | 真机结果 |
|---|---|
| 1. 证据里的 `placement.host` 不是 `local` | `gpu-01` / `autodl-container-…` |
| 2. 实验进程确实在远端 | 脚本自报 `platform.system() == "Linux"`，控制节点是 Windows |
| 3. 切断通道后远端进程消失 | 直接 kill 掉本地 ssh 进程，5 秒后远端 `pgrep` 为空 |
| 4. 产物真的回到本地被打分 | `pred.npy`（二进制，非源码后缀）逐字节回到评估器手里 |

**本地模拟一条都抓不到的四个缺陷。** 这四个的共同点是：它们全都发生在
"本地子进程扮演远端"这条缝的**外面**，而且没有一个会报错。

**（一）Win32-OpenSSH 把远端命令静默截断在 8189 字节。**
退出码仍是 0，远端 bash 只抱怨引号没配对——看起来像转义写错了，不像"太长了"。
把 13 KiB 的 agent 源码 base64 进命令行是 18 KiB，从 Windows 上必断。边界是量
出来的：发 8180 字节到得了 8180，发 8190 只到 8186，再往上永远是 8186。
改成两级加载器（`-c` 里 76 字节，源码走 stdin 头一段），并在 `ssh_argv` 里加一道
本地上限——越界当场红，不到那边去断。

**（二）注入给 agent 的 `Runtime:` 块全是缺省值。**
握手消息只有 pid 与协议版本，事实（os/shell/python/PATH/uv/GPU）从来没被并进
`channel.ready`。于是那份块显示 `Python: missing`、OS 猜成 `Linux`、GPU 型号写成
`GPU`、并且无条件教 `uv add`——一份看起来完全正常、其实一个字段都不是真的描述。
它不报错，只让 agent 按错的前提写代码。改为握手时就问一次 `probe` 并并入
`ready`：预检看到的和实验期注入的从此是同一份。

**（三）非交互式 ssh 不读 `~/.bashrc`，于是远端没有 `python`。**
容器把解释器装在 `/root/miniconda3/bin`，那一段 PATH 是 `.bashrc` 加的。
交互登录一切正常，`ssh host python3 -V` 直接 command not found。远端子进程原本
根本没有 PATH 这一项，第一条 `python train.py` 就死。改为远端在 `probe` 里把自己
解释器的 bin 目录顶到最前、连同分隔符一起报上来，控制节点据此拼 PATH——和本地
执行器把环境根 venv 前置是同一套规则。

**（四）掉线的原因被丢掉了。**
`SshTransport` 开了 stderr 管道却没人读，于是 `Permission denied (publickey)`、
`Connection refused`、远端 shell 的语法报错，全部塌成同一句
`remote channel closed unexpectedly`。三种处置方式完全不同的故障长成一个样，
排查只能靠猜。而且不读还有第二个问题：管道写满之后 ssh 会阻塞在写 stderr 上，
表现成"连上了但没反应"。改为后台抽干、只留最后 4 KiB，掉线时附在错误后面。

**四个缺陷各配了一条回退即红的用例**（截断上限、握手带事实、PATH 来自远端、
掉线带原因）。真机脚本本身没有进仓库：它需要一台配好别名的机器，进 CI 只会变成
一条常年跳过的用例。

### 11.5 真机验收之后补的三件事（2026-08-20）

**（一）`Athena-cli compute --check`。**
验一台机器本来只能靠起一整轮研究。上面四个缺陷里有三个本可以在一条命令里当场
现形。这条命令连上 `[compute]` 里的每台机器，报出：GPU 清单与当前占用、解释器
版本与路径、**已装的关键包**、scratch 余量、上一轮崩溃遗留的租约目录，以及
**会被原样注入给 agent 的那段 Runtime 原文**。最后一项是它真正的价值——那段
文字是最容易悄悄说谎的地方，而它说谎不报错，只让 agent 按错的前提写一整轮代码。
`--data` 时还回答「这台机器装不装得下这份数据集」。不占卡、不留东西，几秒出结果。

**（二）归还租约时删掉远端工作区。**
原本不删。每个跑完的 Plan 在 scratch 上留一份工作区副本，而同一块盘还得装数据集
——一条无界的磁盘泄漏，表现是"实验莫名其妙失败"。删得起，是因为该留的都已经在
控制节点：源码经镜像回到本地 worktree（可信修订据此提交），产出经 `collect_outputs`
回来了。**留在远端的大文件会跟着一起没**，所以它们在删之前被逐条记进日志——
证据里的 `remote_only_paths` 说的是"产出过但没取回"，不是"还能去拿"。
数据集不在此列：它是机器级共享物，按内容寻址跨租约复用，租约只 pin 它。
崩溃那一路没有归还可言，所以遗留目录由（一）报出来，而不是开跑时自动清——
自动清会在多控制节点下删掉别人正在用的租约（§九·6 只是假设单控制节点，没有强制）。

**（三）远端只用现成的解释器，明写。**
原本 `describe` 无条件教 `uv add --project "$ATHENA_ENV_ROOT"`。两处错：真机上
第一台机器就没有 uv，而那个变量指向的还是一个我们凭空建出来的空目录。当时的补法
是按机器上有 uv 还是 pip 来给不同建议——那是把问题往后推了一步。现在收成一条明确
的取舍：**GPU 机上的解释器现成什么样就什么样，Athena 不在那边建环境、不装依赖。**

理由三条：装依赖要写盘、要联网、可能要 sudo，还会在一台可能被别人共用的机器上改
全局状态；半装上的依赖会让下一个租到这台机器的实验跑在一份谁也说不清的环境里，
而证据里不留痕迹；而且这条路本来就不通——真机上第一台机器连 uv 都没有。

代价是 agent 只能用现成的包，所以**必须告诉它现成的是哪些**。`probe` 现在报出
关键包的版本（只读发行元数据，不 `import`——`import torch` 要好几秒，还会在一张
没租出去的卡上初始化 CUDA 上下文），Runtime 块里因此有一行
`Installed: torch 2.5.1+cu124, numpy 2.1.3, …` 和一句「装不了，缺了就换做法」。
相应地：远端不再有 `ATHENA_ENV_ROOT`，不再建 `<scratch>/env`，PATH 里也不再前置
任何我们从不创建的 `.venv/bin`。

三件事各配了回退即红的用例，并在同一台真机上复验：`compute --check` 当场报出了
上一轮遗留的租约目录，修好之后同一条命令确认它随归还消失、而数据集留下了。

### 11.6 还没做的

- **P5**：`SlurmLauncher` / `DockerLauncher`。后者是第八节"这不是沙箱"的唯一真解。
- **远端直取**（`fetch_command`）：§4.8 第 1 条说数据应当优先让 GPU 机自己去拉。
  现在只实现了本地推送。没有上游 URL 的任务用不上，有的话这是最大的一笔节省。
- **租约跨进程互斥**（§九·6）：现在限定单控制节点，但没有强制。
- **GPU 利用率采样**（§七）：`nvidia-smi` 的事实只在注册时取了一次，没有常设采样。
- **多机与排队**：真机验收只跑了一台机器（见 11.4）。`homogeneous` 放置、同型号
  约束、排队与超时、数据亲和这几条，仍然只有本地多机模拟的覆盖。
- **大数据集**：真机上分发的是 18 字节。断点续传与内容校验的逻辑覆盖是够的，
  但"推 50 GB 要多久、控制节点掉线会怎样"没有量过。
