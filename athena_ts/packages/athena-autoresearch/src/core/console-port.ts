export interface CompileConsoleOutput {
  ok: boolean
  consoleText: string
  logRef: string
  compiler: string
}

export interface ConsolePort {
  info(msg: string): void
  error(msg: string): void
  compileOutput(output: CompileConsoleOutput): void
}

export class NullConsolePort implements ConsolePort {
  info(_msg: string): void {}
  error(_msg: string): void {}
  compileOutput(_output: CompileConsoleOutput): void {}
}

export class MemoryConsolePort implements ConsolePort {
  readonly lines: string[] = []
  readonly compileOutputs: CompileConsoleOutput[] = []

  info(msg: string): void {
    this.lines.push(msg)
  }

  error(msg: string): void {
    this.lines.push(`ERROR: ${msg}`)
  }

  compileOutput(output: CompileConsoleOutput): void {
    this.compileOutputs.push(output)
    this.lines.push(output.consoleText)
  }
}
