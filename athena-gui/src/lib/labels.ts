/** Human-readable Chinese labels for backend enums (raw values stay the source of truth). */

export const PHASE_LABEL: Record<string, string> = {
  idle: "未开始",
  PREPARE: "准备",
  SEARCH: "搜索",
  VALIDATE: "验证",
  FINAL: "最终评估",
  COMPLETED: "已完成",
};

export const STATUS_LABEL: Record<string, string> = {
  idle: "空闲",
  running: "运行中",
  paused: "已暂停",
  completed: "已结束",
  error: "出错",
};

export const TASK_TYPE_LABEL: Record<string, string> = {
  classification: "分类",
  binary_classification: "二分类",
  multiclass_classification: "多分类",
  regression: "回归",
  forecasting: "时序预测",
  time_series: "时间序列",
  clustering: "聚类",
  ranking: "排序",
  detection: "检测",
  segmentation: "分割",
  generation: "生成",
  other: "其他",
};

export const DIRECTION_LABEL: Record<string, string> = {
  maximize: "越大越好",
  minimize: "越小越好",
};

export function phaseLabel(phase: string | null | undefined): string {
  if (!phase) return PHASE_LABEL.idle;
  return PHASE_LABEL[phase] ?? phase;
}

export function statusLabel(status: string): string {
  return STATUS_LABEL[status] ?? status;
}

export function taskTypeLabel(taskType: string | null | undefined): string | null {
  if (!taskType) return null;
  return TASK_TYPE_LABEL[taskType] ?? taskType;
}

/** Format a metric value compactly (4 decimals, or "—"). */
export function formatMetric(value: number | null | undefined): string {
  return typeof value === "number" && Number.isFinite(value) ? value.toFixed(4) : "—";
}

/** Shorten long experiment ids for compact displays. */
export function shortId(id: string, keep = 8): string {
  return id.length > keep + 4 ? `…${id.slice(-keep)}` : id;
}
