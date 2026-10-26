/**
 * 测试用最小 pi 扩展（由 tests/test_agent_runtime.py 复制到临时
 * PI_CODING_AGENT_DIR/extensions/thesis-tools.ts，用于满足运行时的扩展存在性检查）。
 *
 * 它不注册任何工具：live 测试只用内置 read 工具验证 tool_start/tool_end 映射；
 * 未注册的 thesis_* 工具名会被 pi 静默忽略（不会导致启动失败）。
 */
export default function (_pi: unknown) {
  // no-op
}
