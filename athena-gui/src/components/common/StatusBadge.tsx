/** Experiment lifecycle → badge tone + Chinese label (mirrors the backend states). */
export const EXPERIMENT_STATUS: Record<string, { tone: string; label: string }> = {
  PENDING: { tone: "neutral", label: "等待中" },
  RUNNING: { tone: "info", label: "运行中" },
  SUCCEEDED: { tone: "success", label: "成功" },
  FAILED: { tone: "danger", label: "失败" },
  CANCELLED: { tone: "neutral", label: "已取消" },
};

/** Hypothesis lifecycle → badge tone + Chinese label. */
export const HYPOTHESIS_STATUS: Record<string, { tone: string; label: string }> = {
  PROPOSED: { tone: "accent", label: "待验证" },
  SUPPORTED: { tone: "success", label: "已支持" },
  REFUTED: { tone: "danger", label: "已证伪" },
  INCONCLUSIVE: { tone: "warning", label: "无定论" },
  REJECTED: { tone: "neutral", label: "已否决" },
};

/** Shared status badge for experiment (default) or hypothesis lifecycle states. */
export function StatusBadge({
  status,
  kind = "experiment",
}: {
  status: string;
  kind?: "experiment" | "hypothesis";
}) {
  const table = kind === "hypothesis" ? HYPOTHESIS_STATUS : EXPERIMENT_STATUS;
  const meta = table[status] ?? { tone: "neutral", label: status };
  return (
    <span className={`badge badge--${meta.tone}`} title={status}>
      {status === "RUNNING" && <span className="badge__dot" style={{ animation: "pulse-dot 1.4s ease infinite" }} />}
      {meta.label}
    </span>
  );
}
