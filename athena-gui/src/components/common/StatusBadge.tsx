import { Label, type LabelColorOptions } from "@primer/react";

/** 实验生命周期状态 → Primer Label 配色。 */
const STATUS_VARIANT: Record<string, LabelColorOptions> = {
  PENDING: "accent",
  RUNNING: "attention",
  SUCCEEDED: "success",
  FAILED: "danger",
  CANCELLED: "secondary",
};

/** 实验生命周期状态标签；未知状态显示为 secondary。 */
export function StatusBadge({ status }: { status: string }) {
  return <Label variant={STATUS_VARIANT[status] ?? "secondary"}>{status}</Label>;
}
