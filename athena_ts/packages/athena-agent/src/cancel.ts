/**
 * ``asyncio.Event`` / ``asyncio.CancelledError`` 的最小 TS 等价。
 *
 * Python 侧以 ``asyncio.Event`` 作为取消信号（``is_set``/``set``/``wait``）贯穿
 * agent 运行时与工具上下文；TS 无内建等价物，故提供 ``CancellationToken``。
 * ``CancelledError`` 对应 ``asyncio.CancelledError``（在 Python 继承 BaseException，
 * 不被 ``except Exception`` 捕获）——工具/运行时据此区分「取消」与「业务错误」。
 */

/** 可等待、可置位的取消令牌（等价 ``asyncio.Event``）。 */
export class CancellationToken {
  private _set = false
  private _waiters: Array<() => void> = []

  /** 是否已置位（等价 ``event.is_set()``）。 */
  isSet(): boolean {
    return this._set
  }

  /** 置位并唤醒所有等待者（等价 ``event.set()``）。 */
  set(): void {
    if (this._set) return
    this._set = true
    const waiters = this._waiters
    this._waiters = []
    for (const resolve of waiters) resolve()
  }

  /** 等待置位（等价 ``await event.wait()``）。 */
  wait(): Promise<void> {
    if (this._set) return Promise.resolve()
    return new Promise((resolve) => this._waiters.push(resolve))
  }
}

/** 取消异常（等价 ``asyncio.CancelledError``）。 */
export class CancelledError extends Error {
  constructor(message = "cancelled") {
    super(message)
    this.name = "CancelledError"
  }
}
