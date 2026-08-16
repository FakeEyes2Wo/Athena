export interface Provider {
  readonly id: string
  readonly version: string
  readonly capabilities: string[]
}

export class ProviderRegistry {
  private readonly providers = new Map<string, Provider>()

  register<T extends Provider>(provider: T): T {
    if (this.providers.has(provider.id)) {
      throw new Error(`duplicate provider: ${provider.id}`)
    }
    this.providers.set(provider.id, provider)
    return provider
  }

  has(id: string): boolean {
    return this.providers.has(id)
  }

  get<T extends Provider>(id: string): T {
    const provider = this.providers.get(id)
    if (!provider) {
      throw new Error(`unknown provider: ${id}`)
    }
    return provider as T
  }

  list(capability?: string): Provider[] {
    return [...this.providers.values()].filter(
      (provider) => !capability || provider.capabilities.includes(capability),
    )
  }
}
