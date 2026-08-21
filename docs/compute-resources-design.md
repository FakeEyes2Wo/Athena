# Compute Resources：远程 GPU 执行设计

Status: current
Owner: Athena maintainers
Last verified: 2026-08-21
Source of truth: `src/athena/execution/`、`test/unit/execution/`、`test/integration/research/test_remote_experiment.py`

本文是 compute resources 这一层的**当前设计**：它解决什么、切在哪、每个部件为什么长这样、
不做什么，以及后续需求。逐日的实现记录与被推翻的设计留在
[远程 GPU 执行：需求分析与架构设计](architecture/2026-08-19-remote-gpu-execution-design.md)，
本文不重复。

## 一、要解决的问题

Athena 的实验此前只能跑在控制节点自己身上。控制节点是一台 Windows 笔记本，没有 GPU，
于是"跑一个需要 GPU 的实验"这件事根本不成立。

要的不是"能 ssh 过去"，而是：

- agent 的**调试回路和执行回路在同一台机器上**。agent 的循环是「写脚本 → 跑 → 读报错 → 改」，
  只把 manifest 远程化、`shell_command` 留在本地，agent 会先用 `shell_command` 试跑、
  看到 `CUDA not available`，然后写出一份 CPU 代码。
- **绝不静默降级**。拿不到 GPU 就排队或明确失败。偷偷退回本地 CPU 会跑完、会出分、
  日志也齐，但那不是你要的实验，而且全链路是绿的——没有任何一层会报错。
- **硬件进证据**。A 臂拿 A100、B 臂拿 3090，在墙钟受限的实验里分数不可比；不记下来就无从发现。

## 二、切在哪：一道协议缝

界线是 `ExecutionBackend`（`src/athena/execution/backend.py`）。**界线之上完全看不到远程，
界线之下完全彻底。**

```
Supervisor / PlanRunner / agent 的 shell_command
        │   全部经 ExecutionRuntime，不知道命令跑在哪台机器
        ▼
┌───────────────────── ExecutionBackend（协议） ─────────────────────┐
│  name  describe  env_ref  ensure_environment  run  collect_outputs  aclose │
└─────────┬────────────────────────────────────────┬─────────────────┘
          │                                        │
    LocalBackend                            MirroredBackend
    （控制节点自己跑）                       ├─ SshBackend       在远端执行命令
                                            └─ WorkspaceMirror  文件怎么两边搬
```

`describe` 单独进协议，是因为它守着一条不变式：**模型看到的环境描述必须来自真正执行命令
的那台机器**。本机是 Windows/PowerShell 而远端是 Linux/bash，描述说错了，模型会老老实实
给 bash 写 PowerShell，报错只显示成语法错误。

`SshBackend` 故意**不是**完整的 `ExecutionBackend`——它少一个 `collect_outputs`。远程执行绕
不开「文件怎么在两台机器之间搬」，而搬法有策略、有代价，必须由 `MirroredBackend` 显式包一
层才算完整。留一个空的 `collect_outputs` 会让「产出没拉回来」变成一次静默的空目录，而不是
一个类型错误。

## 三、模块地图

| 文件 | 行数 | 职责 |
|---|---:|---|
| `execution/backend.py` | 161 | 协议本身 + `LocalBackend` |
| `execution/pool.py` | 435 | 算力池、租约、放置策略、预检 |
| `execution/compute_config.py` | 141 | `config.toml` 的 `[compute]` |
| `execution/check.py` | 271 | `Athena-cli compute --check` 自检 |
| `execution/remote/channel.py` | 569 | 常驻通道：请求/响应、流式、分块传输 |
| `execution/remote/agent.py` | 536 | **跑在 GPU 机上的那一半**，纯 stdlib |
| `execution/remote/ssh.py` | 439 | ssh 传输 + `SshBackend` |
| `execution/remote/mirror.py` | 214 | 工作区镜像（增量推、按需拉） |
| `execution/remote/dataset.py` | 235 | 数据集分发（内容寻址、续传、完成标记） |
| `execution/remote/mirrored.py` | 157 | 把镜像策略套在 `SshBackend` 上 |

## 四、核心机制

### 4.1 租约：粒度是 Plan，不是命令

一个 Plan 拿到租约后，它的**全部**命令都落在同一台机器、同一个目录。

按命令分配会让工作区状态在两台机器之间分叉，而这种分叉不报错，只是结果不对。

```
Plan 开始 ──▶ GpuPool.acquire()
                ├─ 选机器（同型号约束 > 数据亲和 > pack/spread）
                ├─ 建通道、建远端工作区
                └─ 分发数据集（这台机器已有就复用）
              ⋯ 该 Plan 的每条命令都落在这台机器 ⋯
Plan 结算 ──▶ GpuPool.release()
                ├─ 删远端工作区（remote_only 的大文件逐条进日志）
                ├─ 关通道 → 远端 stdin EOF → 它杀掉自己起过的所有进程组
                └─ 卡回池子，唤醒排队的 Plan
```

归还挂在 **Plan 结算**上（`Supervisor.on_plan_settled`），不是 runtime 关闭时。等到关闭才还，
等于每个跑完的 Plan 都继续占着一张卡，池子会在第 N 个实验上无谓地耗尽。

### 4.2 一份租约一条常驻通道

不是"一条命令一个 ssh"。Win32-OpenSSH 不支持 ControlMaster 连接复用，这条设计绕开了它，
而且两端行为一致：

- 握手只付一次，摊到一个 Plan 的几十上百条命令上。
- 取消有地方可送：`cancel` 消息直接杀掉远端 `setsid` 出来的进程组。
- **孤儿有人收**：远端脚本的 stdin 就是 SSH 通道，连接一断即 EOF，它醒来杀掉所有进程组再退出。
  控制节点是 Windows 笔记本时，休眠/漫游/重启是日常，不是边缘情况。

协议是一行一条 JSON：

```
控制节点                                     GPU 机（agent.py）
   │  长度前缀 + agent.py 源码  ───────────▶  │  exec 起来，常驻
   │  ◀───────────────────────  {"op":"ready"}
   │  {"op":"probe"}           ───────────▶  │
   │  ◀───────  {os, shell, python, path, packages, gpus, ...}
   │  {"op":"spawn", argv, cwd, env} ─────▶  │  setsid + Popen
   │  ◀───────  {"op":"out", fd, b64}   （流式：read1，有多少给多少）
   │  ◀───────  {"op":"exit", code}
   │  其余：put / get / manifest / remove / mkdir / space / cancel
```

**源码走 stdin 的头一段，不走命令行。** 这是真机量出来的硬限制：Win32-OpenSSH 9.5 把远端命令
**静默截断在 8189 字节**——退出码仍是 0，远端 bash 只抱怨引号没配对。13 KiB 的源码 base64 后
约 18 KiB，在 Linux 控制节点上能过，从 Windows 上必然断，而断法完全不像"太长了"。
`ssh_argv()` 因此在本地也守一道 4096 字节的上限，越界当场抛错。

也**不能用 `python3 -`**：那会让解释器把整个 stdin 当程序读到 EOF，而 stdin 正是协议通道本身，
同时是「断线即 EOF、自己清场」这条机制的全部依据。所以用长度前缀：读满 N 字节就停手，
剩下的原样留在同一个 `BufferedReader` 里给 agent 接着用。

### 4.3 事实来自机器，不来自配置文件

`RemoteChannel.open()` 在握手之后**立刻**发一次 `probe` 并把结果并进 `channel.ready`。
预检看到的、`compute --check` 打出来的、注入给 agent 的 Runtime 块，是同一份数据。

不这么做的代价是真机上量到的：`ready` 里没有 python，于是 Runtime 块显示 `Python: missing`、
OS 退回默认值 `Linux`、PATH 拼成空——一份看起来完全正常、其实全是缺省值的运行时描述。
那种错法不会报错，只会让远端第一条命令 command not found，而信息指不到原因。

PATH 也必须问远端要：**非交互式 ssh 不读 `~/.bashrc`**，容器里 conda 那一段 PATH 因此不在。
真机上的表现是 `ssh host python3 -V` 直接 command not found，而交互登录一切正常。
远端 agent 在 `probe` 里把自己解释器的 bin 目录顶到最前再报上来。

### 4.4 远端只用现成的解释器

**Athena 不在 GPU 机上装任何东西。** 这是一个明确的取舍，不是没做完：

- 装依赖要写盘、要联网、可能要 sudo，还会在一台可能被别人共用的机器上改全局状态。
- 装包的失败模式很脏：半装上的依赖会让下一个租到这台机器的实验跑在一份谁也说不清的环境里，
  而证据里不会留下任何痕迹。

代价是 agent 只能用现成的包，所以**必须告诉它现成的是哪些**——否则它会拿一轮去试 import、
再拿一轮去试装、最后拿一轮猜为什么装不上。`probe` 因此报出一批关键包的版本，
Runtime 块里写成：

```
- Installed: torch 2.5.1+cu124, numpy 2.1.2, pandas 2.2.3, ...
- You cannot install packages on this host, and Athena will not do it for you.
  If an import is missing, change the approach instead.
```

版本只读发行元数据，**不 import**：`import torch` 要好几秒，还会初始化 CUDA 上下文、
在一张还没租出去的卡上占住显存。

### 4.5 工作区：本地为准的镜像

本地 worktree 是 git 提交的依据（`LocalGitWorkspace.commit` 在本地算 diff），
`read_file`/`write_file` 也绑死在本地目录。所以是镜像，不是"远端为家"：改动面最小——
git 提交、文件工具、agent 的心智模型全部不变。

- **每条命令之前**推增量。agent 刚写的脚本必须先到远端，否则命令跑的是上一版——
  这种错完全不报错，只是结果不对。
- **每条命令之后**只拉回源码类小文件（判据是「进不进 git 提交」，不是「是不是文本」），
  外加 manifest 声明过的产出目录。刻意不整棵拉：那会把 checkpoint、特征缓存一起拖回来。
- 留在远端的东西必须**被点名**（`remote_only`）并进证据。不点名的话 agent 会以为文件丢了并重跑。

增量判据是 `(相对路径, size, sha256)` 的集合差，**不用 mtime**：跨时区、跨文件系统、
跨传输方式的 mtime 都不可靠，而这里的一端是 Windows 笔记本。

### 4.6 数据集分发：能证明它是完整的

> 半个数据集是本设计里最阴的失败。脚本照跑、分数照出、只是少了一半样本，没有任何一层会报错。

每个决定都围绕"能不能证明它完整"：

- **内容寻址**：目录名是清单的哈希。数据变了就是另一个目录，不存在"覆盖了一半"。
- **完成标记**：全部文件逐个校验通过之后才写 `.athena-complete.json`。没有标记一律当作不存在。
- **断点续传**：中断后重来只补缺的那些。
- **事后校验**：标记在但内容对不上（有人手删了一个文件）必须**报错**，不能照常跑。

一台机器一份，不是一个 Plan 一份：数据是不可变共享物，租约只 pin 它。

### 4.7 传输：瓶颈是往返次数，不是带宽

分块上传原本一块一等，于是吞吐 = 块大小 ÷ 往返时延，与带宽无关。真机实测：

| | 改前 | 改后 | 同链路裸 `scp` |
|---|---:|---:|---:|
| 数据集分发 | 1.9 MiB/s | **4.3 MiB/s** | 5.9 MiB/s |
| 推 50 个源码文件 | 1.69 s | **0.47 s** | — |

64 KiB ÷ 31 ms ≈ 2 MiB/s，实测 1.9——时间几乎全花在等确认上。两处修法：

- **块级窗口**（`TRANSFER_WINDOW = 16`）：一次允许 16 块在飞而不等确认，上限抬到
  `窗口 × 块 ÷ RTT`。顺序不受影响（写是串行的，远端读循环也顺序派发），`append` 语义仍成立。
- **文件级并发**（`TRANSFER_FILES = 4`）：文件之间互不相干。

两个上界都是有意的：飞行中的字节数 = 窗口 × 块（base64 后约 ×1.33）。无界地灌会把内存和
管道缓冲一起吃掉，而且失去背压——`drain` 正是靠管道满了才挡一下。

4.3 / 5.9 = 73% ≈ 1/1.33，正好是 base64 的膨胀比——**已经打到链路本身的上限**，
所以到此为止，不再上二进制分帧。

### 4.8 输出：留头留尾，全文另存

`BoundedOutput`（`execution/runtime.py`）本地与远程共用一份。

一次训练打几千行日志，前面是配置回显，**最后才是 traceback / 最终指标 / OOM**。
保头砍尾恰好把唯一有用的那一段删掉，而且流读过就没了——不像文件还能再读一次。
所以头 1/3、尾 2/3，中间用省略标记接上，标记里写清楚断了多少、去哪儿取全的。

`truncated: true` 配 `output_ref` 是一个契约：那个 ref 承诺"完整输出在这里"，就必须真的完整。
远端此前存的是被砍过的那份，等于给了一个骗人的 ref。

### 4.9 算力是怎么被感知和使用的：问 → 排 → 绑 → 记

| 阶段 | 谁在做 | 做什么 |
|---|---|---|
| **问** | `GpuPool.preflight()` | 注册时连上每台机器一次问清事实，不过就当场红 |
| **排** | `GpuPool.acquire()` | 同型号约束 → 数据亲和 → pack/spread，拿不到就排队 |
| **绑** | `PhaseRunner` | Plan 的 `ExecutionRuntime` 换成租约的 backend，之后所有命令都在那台机器 |
| **记** | `Lease.placement()` | 主机、卡号、卡型号、排队秒数、数据集 id、`remote_only_paths` 进证据 |

预检在**注册时**做，不是实验时。这类系统最典型的浪费是：跑了两小时 PREPARE，
在第一个实验才发现远端跑不了。

数据亲和优先于放置策略：落到没有数据的机器上要先付一次完整分发，大数据集下这一笔以小时计，
而 pack/spread 的差别只是几个百分点的利用率。与 homogeneous 不冲突——同型号的过滤在候选筛选
里就做完了，亲和只在同型号的机器之间排序（分发只是慢，异构是结果不可比）。

## 五、必须成立的不变式

1. Runtime 块描述的是**执行命令的那台机器**。
2. 一个 Plan 的所有命令落在同一台机器、同一个目录。
3. 拿不到算力 → 排队或报错，**永不静默退回本地**。
4. 硬件进证据（`placement`）。
5. 可信评估器与测试标签**永远留在控制节点**，远端只产出 `predictions/`。
6. 通道一断，远端把自己起过的每一个进程组都带走。
7. 数据集要么完整、要么当作不存在。

## 六、配置与使用

配置在仓库根的 `config.toml`（已被 `.gitignore` 忽略）：

```toml
[compute]
mode = "ssh"              # local | ssh
placement = "pack"        # pack | spread | homogeneous
fallback = "never"        # never | ask，**没有 silent**
gpus_per_experiment = 1
queue_timeout_s = 3600

[[compute.hosts]]
name = "gpu-01"
ssh = "gpu01.lab"         # ~/.ssh/config 的 Host 别名，不是 user@ip
scratch = "/root/autodl-tmp/athena"
python = "/root/miniconda3/bin/python"
gpus = "auto"             # "auto" 或 [0, 1]
max_leases = 1
```

**配置里只写 Host 别名。** 连接的复杂度（跳板、端口、密钥、known_hosts）留在唯一有资格管它
的地方 `~/.ssh/config`，Athena 的任何配置文件里因此永远不会出现凭据。写成 `user@ip` 直接报错——
一旦允许，端口、密钥路径、跳板设置就会跟着搬进来，凭据管理从此有两个地方。

`fallback` 没有 `silent` 这个取值，是产品判断，不是没实现。

用法：

```bash
# 1. 先自检。不占卡、不留下任何东西，几秒钟给出答案
Athena-cli compute --check --data ./data

# 2. 跑。--compute 临时覆盖 config.toml
Athena-cli run --project ./proj --data ./data --compute ssh
```

`compute --check` 刻意不只报"连上了"，而是把**注入给 agent 的那段 Runtime 原文**一起打出来：
它是最容易悄悄说谎的一处，而它说谎的代价是 agent 按错的前提写一整轮代码。顺带回答两个只有
站在机器上才知道的问题：scratch 还剩不剩得下一份数据集，以及有没有上一轮崩溃留下、
再没人会清的租约目录（正常归还会自己删掉）。

## 七、限制（明写，不藏）

1. **这不是沙箱。** agent 在 GPU 机上以你的身份跑任意代码。缓解手段只有 `ForwardAgent=no`
   （不让它顺手拿到本地的 ssh 身份）和 scratch 目录约定。真正的答案是容器化。
2. **只记账，不隔离。** 共享机器上别的用户随时能在"你的"卡上起进程，Athena 拦不住。
   缓解手段是把观测到的争用写进证据，让不可比的比较可被发现，而不是假装没发生。
3. **跨进程没有互斥。** 两个 Athena 进程指向同一台机器会互相不知道对方占了卡。
4. **多机只在本地模拟里验过。** `homogeneous` 放置、同型号约束、排队与超时、数据亲和，
   都还没在两台真机上跑过。
5. **`ask` 降级尚未接到交互层**，目前等价于 `never`。

## 八、测试方案与结果

思路是：**真正的 ssh 那一跳在开发机上无法验证，它之外的全部都可以**——只要把同一份
`agent.py` 用本地子进程拉起来。所以这里跑的不是 mock：真的起进程、真的杀进程组、真的读回字节。
两个传输拉起 agent 的方式**必须是同一套**（同一个加载器、同一段 stdin 前导），否则本地那
一百多条用例覆盖的就不是真机跑的那条路径。留在覆盖之外的只剩 ssh 命令行本身——它短到可以
一眼看完，且由 `test_ssh_backend.py` 逐项断言。

| 层 | 文件 | 覆盖 |
|---|---|---|
| 协议 | `test_remote_channel.py` | 握手、流式、退出码、取消、超时、分块读写、清单、断线清场、传输窗口 |
| 后端 | `test_ssh_backend.py` | ssh 命令行、截断上限、Runtime 块、PATH 来源、输出截断与全文 |
| 池 | `test_pool.py` | 预检、分配、排队、放置策略、归还与工作区清理 |
| 数据 | `test_dataset_staging.py` | 内容寻址、续传、完成标记、事后校验、流式不整读 |
| 配置 | `test_compute_config.py` | 别名而非凭据、无 silent 降级、错误定位到第几台机器 |
| 自检 | `test_compute_check.py` | 逐台机器、单台异常不拖垮整轮 |
| 端到端 | `test_remote_experiment.py` | 一个 Plan 从取租约到结算归还的全程 |

纪律：**每个修复都要有一个"把修复回退就变红"的测试**，且断言不能对常量恒真
（把 `TRANSFER_WINDOW` 设成 1 仍然通过 = 这个断言什么也没守住）。

结果（2026-08-21）：

- 执行层与远程实验：**155 通过**
- 全量：**1553 通过**；5 个失败与 2 个收集错误全部是本分支之前就存在的
  （`test/` 缺 `__init__.py`，被 CPython 标准库的 `test` 包遮蔽）
- `black --check` 与 `scripts/check_code_style.py --enforce-docstrings` 对
  `src/athena/execution` 与 `src/athena/cli.py` 全部通过

真机验收（AutoDL，RTX 3090）已完成：握手、probe、流式、取消、超时、镜像增量、数据集分发、
租约归还与远端清场全部跑通，并因此改掉了四个本地模拟抓不到的缺陷（命令静默截断、
Runtime 块全是缺省值、非交互式 PATH、ssh stderr 从不抽干）。

## 九、后续需求

按优先级。前三条是"要让这套东西真正好用"必须做的，后面是补齐。

### 9.1 让 GPU 机自己从对象存储拉数据（P1）

现在数据集只能从控制节点推上去，4.3 MiB/s 已经是这条链路的上限。同一台 GPU 机自己从
镜像站下载实测 **14.5 MiB/s**，而且推送期间控制节点不能休眠、不能断网。

做法：在 `DatasetSpec` 旁边加一个 `fetch_command` 缝——数据若有上游来源（Kaggle 比赛、
一个 URL、对象存储），远端自己去拉，控制节点只负责校验清单。本地推送退化成没有上游时的兜底。

50 GiB 的三种耗时：控制节点推（改前）7.5 h → 控制节点推（改后）3.3 h → 远端自拉 59 min。

### 9.2 接入第二台机器（P1）

`homogeneous` 放置、同型号约束、排队与超时、数据亲和目前只有本地模拟覆盖。需要在两台真机上
验证的具体问题：跨机的同型号判定是否稳定、排队唤醒有没有惊群、数据亲和在两台都冷时的行为。

### 9.3 容器化（P2）

这是"不是沙箱"和"只记账不隔离"两条限制的唯一真答案。`ContainerLauncher` 的位置在设计里已经
留好（`docker run` / `enroot`），接口与 `SshBackend` 一致。

### 9.4 共享数据存储（P2）

多机之后，"一台机器一份数据"会变成 N 份。可选路径：NFS/Lustre（集群有就直接用）、
S3 兼容对象存储 + 远端拉取（与 9.1 是同一件事）、或 rclone/JuiceFS。选型未定。

### 9.5 补齐（P3）

- `SlurmLauncher`：集群环境用 `sbatch`/`srun` 而不是自己当调度器。
- 跨进程租约互斥：远端 scratch 下的 lock 文件 + 心跳。
- GPU 利用率采样进证据：现在只在 `probe` 时看一眼，看不到实验期的争用。
- `ask` 降级接到交互层。
- `test/__init__.py`：消掉 2 个收集错误。

### 9.6 可以接进来的现成组件

评估过两个，结论与插入位置见
[架构文档 §十二](architecture/2026-08-19-remote-gpu-execution-design.md)：

- **SkyPilot**：接在 Launcher 这条缝上，用来管云上按需实例的申请/释放/竞价回收。
  它替换的是"怎么拿到机器"，不是"拿到之后怎么跑"——通道、镜像、租约都不动。
- **W&B**：接在观测旁路上，**绝不进打分链**。指标的唯一来源必须是可信评估器；
  让 agent 能写 W&B 就等于开了一条它自己报分的路。
