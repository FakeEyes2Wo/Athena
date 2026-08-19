"""远端 supervisor：在 GPU 机上跑的那一半，一份自包含的 stdlib 脚本。

它由控制节点经 ``ssh <host> python3 -c <base64 源码>`` 拉起后常驻，之后双方在同一条
SSH 通道上用「一行一条 JSON」对话。源码走 ``-c`` 而不是 stdin，是因为 stdin 就是通道
本身（也是「断线即 EOF、自己清场」这条机制的全部依据），不能被解释器当程序读掉。**一份租约一条通道**，不是一条命令一个 ssh：

- 握手只付一次，摊到一个 Plan 的几十上百条命令上；Win32-OpenSSH 不支持
  ControlMaster 连接复用，靠这条设计绕开，两端行为还一致。
- 取消有地方可送：``cancel`` 消息直接杀掉自己 ``setsid`` 出来的进程组。
- **孤儿有人收**：本脚本的 stdin 就是 SSH 通道。连接一断即 EOF，它醒来杀掉所有
  进程组再退出。这是显存不被一个跑飞的训练进程永久占住的唯一可靠机制——控制节点
  是 Windows 笔记本时，休眠/漫游/重启都是日常，不是边缘情况。

约束（有意为之，不静默降级）：

- 只用标准库，且不用任何 3.8 以后的语法：远端的 ``python3`` 未必新。
- 进程组语义两个平台各走各的：POSIX 用 ``os.setsid`` + ``os.killpg``，
  Windows 用 ``CREATE_NEW_PROCESS_GROUP`` + ``taskkill /T``（与控制节点本地
  执行器的做法一致）。目标远端是 Linux，但两边都支持让整条协议能在开发机上
  用本地子进程跑真实验证——只剩 ssh 那一跳没被覆盖。
- 二进制安全：子进程输出原样 base64 后回传，解码由控制节点按它自己的策略做。
  协议流与数据流因此不会互相污染。
"""

import base64
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import threading

PROTOCOL_VERSION = 1

# 一次回传的最大原始字节数。太小则消息数爆炸，太大则一条日志行卡住整条通道。
CHUNK_BYTES = 64 * 1024

_POSIX = os.name != "nt"

_out_lock = threading.Lock()
_jobs = {}
_jobs_lock = threading.Lock()


def _send(payload):
    """把一条消息写回控制节点（整行原子写，避免多线程交错）。"""
    line = json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n"
    with _out_lock:
        sys.stdout.buffer.write(line)
        sys.stdout.buffer.flush()


def _fail(request_id, message):
    _send({"op": "error", "id": request_id, "error": str(message)[:2000]})


def _pump(job_id, stream, fd):
    """把一条子进程输出流按块回传，直到 EOF。

    必须用 ``read1`` 而不是 ``read``：``BufferedReader.read(n)`` 会一直等到凑满 n
    字节或者 EOF，于是一条只打印了几十字节的训练日志要等进程结束才出现——看起来
    像流式，实际是「跑完一次性吐出来」。``read1`` 有多少给多少。
    """
    try:
        while True:
            block = stream.read1(CHUNK_BYTES)
            if not block:
                return
            _send(
                {
                    "op": "out",
                    "id": job_id,
                    "fd": fd,
                    "b64": base64.b64encode(block).decode("ascii"),
                }
            )
    finally:
        try:
            stream.close()
        except OSError:
            pass


def _wait(job_id, proc, readers):
    """等子进程结束，把两条流抽干，再回报退出码。"""
    code = proc.wait()
    for reader in readers:
        reader.join()
    with _jobs_lock:
        _jobs.pop(job_id, None)
    _send({"op": "exit", "id": job_id, "code": code})


def _spawn(message):
    """起一个新进程组并流式回传它的输出。"""
    job_id = message["id"]
    argv = message.get("argv")
    command = message.get("command")
    cwd = message.get("cwd") or os.getcwd()
    env = dict(os.environ)
    env.update(message.get("env") or {})

    if not os.path.isdir(cwd):
        _fail(job_id, "workdir does not exist: " + cwd)
        _send({"op": "exit", "id": job_id, "code": -1})
        return
    if argv:
        args = list(argv)
    elif command:
        args = _shell_argv(message.get("shell"), command)
    else:
        _fail(job_id, "spawn needs argv or command")
        _send({"op": "exit", "id": job_id, "code": -1})
        return

    # 新进程组：cancel 与 EOF 清理都要打整棵树，不能只杀直接子进程。
    group = {}
    if _POSIX:
        group["preexec_fn"] = os.setsid
    else:
        group["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        proc = subprocess.Popen(
            args,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **group,
        )
    except OSError as exc:
        _fail(job_id, exc)
        _send({"op": "exit", "id": job_id, "code": -1})
        return

    with _jobs_lock:
        _jobs[job_id] = proc
    _send({"op": "started", "id": job_id, "pid": proc.pid})

    readers = [
        threading.Thread(target=_pump, args=(job_id, proc.stdout, 1), daemon=True),
        threading.Thread(target=_pump, args=(job_id, proc.stderr, 2), daemon=True),
    ]
    for reader in readers:
        reader.start()
    threading.Thread(target=_wait, args=(job_id, proc, readers), daemon=True).start()


def _shell_argv(shell, command):
    """把一条 shell 字符串包成 argv。远端的 shell 由它自己说了算。"""
    if _POSIX:
        return [shell or "/bin/bash", "-c", command]
    found = shutil.which("pwsh") or shutil.which("powershell")
    if found:
        return [found, "-NoProfile", "-NonInteractive", "-Command", command]
    return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", command]


def _kill(proc):
    """杀掉一个进程组；进程已退出时安静返回。"""
    if _POSIX:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
        return
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        pass


def _cancel(message):
    with _jobs_lock:
        proc = _jobs.get(message.get("target") or message["id"])
    if proc is not None:
        _kill(proc)
    _send({"op": "ok", "id": message["id"]})


def _put(message):
    """写一个文件；父目录自动建。"""
    path = message["path"]
    parent = os.path.dirname(path)
    if parent:
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError as exc:
            _fail(message["id"], exc)
            return
    data = base64.b64decode(message.get("b64") or "")
    mode = "ab" if message.get("append") else "wb"
    try:
        with open(path, mode) as handle:
            handle.write(data)
        if message.get("executable"):
            os.chmod(path, 0o755)
    except OSError as exc:
        _fail(message["id"], exc)
        return
    _send({"op": "ok", "id": message["id"]})


def _get(message):
    """读一个文件并分块回传。"""
    path = message["path"]
    try:
        with open(path, "rb") as handle:
            while True:
                block = handle.read(CHUNK_BYTES)
                if not block:
                    break
                _send(
                    {
                        "op": "chunk",
                        "id": message["id"],
                        "b64": base64.b64encode(block).decode("ascii"),
                    }
                )
    except OSError as exc:
        _fail(message["id"], exc)
        return
    _send({"op": "ok", "id": message["id"]})


def _digest(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(CHUNK_BYTES)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _manifest(message):
    """列出一棵子树的 ``(相对路径, 大小, sha256)``。

    增量判据用大小与内容哈希，**不用 mtime**：跨时区、跨文件系统、跨传输方式的
    mtime 都不可靠，而这里的对端是一台 Windows 笔记本。
    """
    root = message["root"]
    excluded = set(message.get("exclude") or [])
    entries = []
    if os.path.isdir(root):
        for base, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in excluded]
            for name in files:
                absolute = os.path.join(base, name)
                relative = os.path.relpath(absolute, root).replace(os.sep, "/")
                try:
                    entries.append(
                        [relative, os.path.getsize(absolute), _digest(absolute)]
                    )
                except OSError:
                    # 竞态删除/权限问题：当作不存在，由上层按缺失处理
                    continue
    _send({"op": "manifest", "id": message["id"], "entries": sorted(entries)})


def _remove(message):
    for path in message.get("paths") or []:
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        else:
            try:
                os.remove(path)
            except OSError:
                pass
    _send({"op": "ok", "id": message["id"]})


def _mkdir(message):
    try:
        os.makedirs(message["path"], exist_ok=True)
    except OSError as exc:
        _fail(message["id"], exc)
        return
    _send({"op": "ok", "id": message["id"]})


def _probe(message):
    """注册期预检要的那些事实，一次问清。

    这类系统最典型的浪费是：跑了两小时的 PREPARE，在第一个实验才发现远端没装 uv。
    """

    def _version(name, *args):
        binary = shutil.which(name)
        if not binary:
            return None
        try:
            out = subprocess.run(
                [binary] + list(args),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        text = out.stdout.decode("utf-8", "replace").strip().splitlines()
        return text[0] if text else ""

    gpus = []
    if shutil.which("nvidia-smi"):
        try:
            out = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=index,name,memory.total,memory.used,utilization.gpu",
                    "--format=csv,noheader,nounits",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=30,
            )
            for line in out.stdout.decode("utf-8", "replace").strip().splitlines():
                parts = [part.strip() for part in line.split(",")]
                if len(parts) >= 5:
                    gpus.append(
                        {
                            "index": int(parts[0]),
                            "name": parts[1],
                            "memory_total_mib": int(parts[2]),
                            "memory_used_mib": int(parts[3]),
                            "utilization_pct": int(parts[4]),
                        }
                    )
        except (OSError, ValueError, subprocess.SubprocessError):
            gpus = []

    shell = shutil.which("bash") or shutil.which("sh")
    _send(
        {
            "op": "probe",
            "id": message["id"],
            "protocol": PROTOCOL_VERSION,
            "os": os.uname().sysname if hasattr(os, "uname") else sys.platform,
            "hostname": os.uname().nodename if hasattr(os, "uname") else "",
            "shell": shell,
            "python": sys.version.split()[0],
            "python_executable": sys.executable,
            "uv": _version("uv", "--version"),
            "git": _version("git", "--version"),
            "nvidia_smi": _version("nvidia-smi", "--version") is not None,
            "gpus": gpus,
            "cwd": os.getcwd(),
        }
    )


_HANDLERS = {
    "spawn": _spawn,
    "cancel": _cancel,
    "put": _put,
    "get": _get,
    "manifest": _manifest,
    "remove": _remove,
    "mkdir": _mkdir,
    "probe": _probe,
}


def _shutdown():
    """通道断了：把自己起过的每一个进程组都带走。"""
    with _jobs_lock:
        procs = list(_jobs.values())
        _jobs.clear()
    for proc in procs:
        _kill(proc)


def main():
    _send({"op": "ready", "protocol": PROTOCOL_VERSION, "pid": os.getpid()})
    try:
        for raw in sys.stdin.buffer:
            raw = raw.strip()
            if not raw:
                continue
            try:
                message = json.loads(raw.decode("utf-8"))
            except ValueError:
                _send({"op": "error", "id": None, "error": "malformed request"})
                continue
            handler = _HANDLERS.get(message.get("op"))
            if handler is None:
                _fail(message.get("id"), "unknown op: " + str(message.get("op")))
                continue
            try:
                handler(message)
            except Exception as exc:  # 单条请求失败不该带走整条通道
                _fail(message.get("id"), repr(exc))
    finally:
        # stdin EOF（含 SSH 断线）→ 清场。这是显存不被孤儿进程占死的关键路径。
        _shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
