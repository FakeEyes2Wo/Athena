import { useMemo } from "react";
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { experimentsList, settingsGet, type ExperimentDetail } from "../lib/tauri-bridge";
import { formatMetric, shortId } from "../lib/labels";
import { useRemoteData } from "../hooks/useRemoteData";
import { EmptyState } from "./common/EmptyState";
import { Icon } from "./common/Icon";
import { EXPERIMENT_STATUS } from "./common/StatusBadge";

interface Point {
  index: number;
  name: string;
  id: string;
  primary: number;
  best: number;
  status: string;
  sota: boolean;
}

function ChartTooltip({ active, payload }: { active?: boolean; payload?: Array<{ payload: Point }> }) {
  if (!active || !payload || payload.length === 0) return null;
  const point = payload[0].payload;
  return (
    <div className="chart-tooltip">
      <div className="chart-tooltip__title">{point.id}</div>
      <div className="chart-tooltip__row">
        <span>主指标</span>
        <strong>{point.primary.toFixed(4)}</strong>
      </div>
      <div className="chart-tooltip__row">
        <span>截至此时最佳</span>
        <strong>{point.best.toFixed(4)}</strong>
      </div>
      <div className="chart-tooltip__row">
        <span>状态</span>
        <strong>{EXPERIMENT_STATUS[point.status]?.label ?? point.status}</strong>
      </div>
      {point.sota && <div className="chart-tooltip__sota">★ 当前 SOTA</div>}
    </div>
  );
}

function toPoints(experiments: ExperimentDetail[], minimize: boolean): Point[] {
  const points: Point[] = [];
  let best: number | null = null;
  for (const item of experiments) {
    const primary = item.experiment.eval?.primary;
    if (typeof primary !== "number") continue;
    best = best === null ? primary : minimize ? Math.min(best, primary) : Math.max(best, primary);
    points.push({
      index: points.length + 1,
      name: shortId(item.experiment.id),
      id: item.experiment.id,
      primary,
      best,
      status: item.experiment.status,
      sota: item.sota,
    });
  }
  return points;
}

/** Primary-metric trend across experiments, with a best-so-far line and the SOTA point. */
export default function MetricChart({ compact = false }: { compact?: boolean }) {
  const { data, error, loading, refresh } = useRemoteData(() => experimentsList().then((res) => res.experiments));
  // The runtime's optimization direction decides what "best so far" means.
  const settings = useRemoteData(() => settingsGet(), { follow: false });
  const minimize = settings.data?.direction === "minimize";
  const points = useMemo(() => toPoints(data ?? [], minimize), [data, minimize]);
  const sota = points.find((point) => point.sota);

  return (
    <div className="detail-panel">
      <div className="panel-toolbar">
        <h3>
          主指标趋势 <span className="panel-toolbar__count">{points.length}</span>
        </h3>
        <button
          type="button"
          className="icon-btn icon-btn--sm"
          onClick={() => void refresh()}
          disabled={loading}
          aria-label="刷新"
          title="刷新"
        >
          <Icon name="refresh" size={14} className={loading ? "icon--spin" : undefined} />
        </button>
      </div>

      {error && (
        <div className="callout callout--error" role="alert">
          <Icon name="alert" size={15} />
          <div className="callout__body">{error}</div>
        </div>
      )}

      {points.length === 0 ? (
        <EmptyState
          icon={loading ? "loader" : "metrics"}
          title={loading ? undefined : "暂无评估指标"}
          message={loading ? "加载中…" : "实验完成评估后，这里会展示主指标随实验推进的变化。"}
        />
      ) : (
        <>
          {!compact && (
            <div className="stat-grid">
              <div className="stat">
                <div className="stat__label">已评估实验</div>
                <div className="stat__value">{points.length}</div>
              </div>
              <div className="stat">
                <div className="stat__label">当前最佳</div>
                <div className="stat__value" style={{ color: "var(--gold)" }}>
                  {formatMetric(points[points.length - 1].best)}
                </div>
              </div>
              <div className="stat">
                <div className="stat__label">SOTA 实验</div>
                <div className="stat__value stat__value--mono">{sota ? shortId(sota.id, 14) : "—"}</div>
              </div>
            </div>
          )}
          <div
            style={{
              width: "100%",
              height: compact ? 240 : 340,
              padding: compact ? "8px 0 0" : "16px 12px 8px 0",
              border: compact ? "none" : "1px solid var(--border)",
              borderRadius: "var(--radius-card)",
              background: compact ? "transparent" : "var(--surface)",
            }}
          >
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={points} margin={{ top: 8, right: 16, bottom: 4, left: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" vertical={false} />
                <XAxis
                  dataKey="index"
                  tick={{ fontSize: 11, fill: "var(--text-tertiary)" }}
                  tickLine={false}
                  axisLine={{ stroke: "var(--border)" }}
                />
                <YAxis
                  tick={{ fontSize: 11, fill: "var(--text-tertiary)" }}
                  tickLine={false}
                  axisLine={false}
                  width={56}
                  domain={["auto", "auto"]}
                  tickFormatter={(value: number) => value.toFixed(3)}
                />
                <Tooltip content={<ChartTooltip />} cursor={{ stroke: "var(--border-strong)" }} />
                <Line
                  type="stepAfter"
                  dataKey="best"
                  stroke="var(--gold)"
                  strokeWidth={1.5}
                  strokeDasharray="4 4"
                  dot={false}
                  activeDot={false}
                  isAnimationActive={false}
                />
                <Line
                  type="linear"
                  dataKey="primary"
                  stroke="var(--accent)"
                  strokeWidth={2}
                  isAnimationActive={false}
                  dot={({ cx, cy, payload }: { cx?: number; cy?: number; payload?: Point }) => {
                    if (cx == null || cy == null || !payload) return <></>;
                    return (
                      <circle
                        key={payload.id}
                        cx={cx}
                        cy={cy}
                        r={payload.sota ? 6 : 3.5}
                        fill={payload.sota ? "var(--gold)" : payload.status === "FAILED" ? "var(--danger)" : "var(--accent)"}
                        stroke="var(--surface)"
                        strokeWidth={2}
                      />
                    );
                  }}
                  activeDot={{ r: 5 }}
                />
              </LineChart>
            </ResponsiveContainer>
          </div>
          <div style={{ display: "flex", gap: 16, fontSize: 12, color: "var(--text-tertiary)" }}>
            <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
              <span style={{ width: 14, height: 2, background: "var(--accent)", borderRadius: 2 }} /> 每次实验
            </span>
            <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
              <span style={{ width: 14, borderTop: "2px dashed var(--gold)" }} /> 截至当前最佳
            </span>
          </div>
        </>
      )}
    </div>
  );
}
