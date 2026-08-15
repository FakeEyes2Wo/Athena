/** Status → badge variant mapping (mirrors the backend experiment lifecycle). */
const STATUS_BADGE: Record<string, string> = {
  PENDING: "badge--info",
  RUNNING: "badge--warning",
  SUCCEEDED: "badge--success",
  FAILED: "badge--danger",
  CANCELLED: "badge--neutral",
};

/** Shared status badge for experiment/hypothesis lifecycle states. */
export function StatusBadge({ status }: { status: string }) {
  return <span className={`badge ${STATUS_BADGE[status] ?? "badge--neutral"}`}>{status}</span>;
}
