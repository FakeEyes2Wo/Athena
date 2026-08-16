// athena-dsh 源码对 @deepseek-ai/* 仅使用 `import type`，运行时不依赖 DSH 包，
// 因此本地 vitest 对拍（npm cordis）可以与其他包一起跑；DSH 实机接线验证另见
// docs/superpowers/specs/2026-08-14-athena-dsh-plugin-design.md。
export default ["packages/*"]
