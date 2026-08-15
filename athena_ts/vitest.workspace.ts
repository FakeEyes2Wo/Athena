// @athena/dsh 依赖 @deepseek-ai/*（DSH 运行时才提供），故默认排除；其测试在 DSH 环境内跑。
export default ["packages/*", "!packages/athena-dsh"]
