import type { LabelColorOptions } from "@primer/react";

/** 假设状态 → Primer Label 配色；研究树、假设图、待选假设、实验列表共用。 */
export const HYPOTHESIS_STATUS_VARIANT: Record<string, LabelColorOptions> = {
  SUPPORTED: "success",
  REFUTED: "danger",
  PROPOSED: "accent",
  REJECTED: "secondary",
  INCONCLUSIVE: "attention",
};

const BORDER_COLOR: Partial<Record<LabelColorOptions, string>> = {
  success: "var(--borderColor-success-emphasis)",
  danger: "var(--borderColor-danger-emphasis)",
  accent: "var(--borderColor-accent-emphasis)",
  attention: "var(--borderColor-attention-emphasis)",
  secondary: "var(--borderColor-emphasis)",
};

/** 假设状态对应的 Label variant；未知状态回落 secondary。 */
export function hypothesisVariant(status: string): LabelColorOptions {
  return HYPOTHESIS_STATUS_VARIANT[status] ?? "secondary";
}

/** 图节点边框与小地图用的状态色（Primer 变量）。 */
export function hypothesisBorderColor(status: string): string {
  return BORDER_COLOR[hypothesisVariant(status)] ?? "var(--borderColor-emphasis)";
}
