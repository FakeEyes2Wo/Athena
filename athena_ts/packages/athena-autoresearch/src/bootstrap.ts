import { GateRunner } from "./core/gate-runner.js"
import { ProviderRegistry } from "./core/provider-registry.js"
import {
  BundledTemplateProvider,
  NativeSvgProvider,
  NoneCompilerProvider,
  NoneLiteratureProvider,
  StubExperimentEngine,
  createBuiltinGates,
} from "./providers/builtins.js"
import { createDefaultStages } from "./stages.js"

export function createDefaultProviders(): ProviderRegistry {
  const registry = new ProviderRegistry()
  registry.register(new NativeSvgProvider())
  registry.register(new StubExperimentEngine())
  registry.register(new BundledTemplateProvider())
  registry.register(new NoneCompilerProvider())
  registry.register(new NoneLiteratureProvider())
  return registry
}

export function createDefaultGateRunner(): GateRunner {
  const gateRunner = new GateRunner()
  for (const gate of createBuiltinGates()) {
    gateRunner.register(gate)
  }
  return gateRunner
}

export { createDefaultStages }
