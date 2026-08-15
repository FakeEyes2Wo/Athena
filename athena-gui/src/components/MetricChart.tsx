import { useCallback, useEffect, useState } from "react";
import { errorMessage } from "../lib/errors";
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { experimentsList, type ExperimentDetail } from "../lib/tauri-bridge";
import { EmptyState } from "./common/EmptyState";

interface Point {
  name: string;
  primary: number;
  status: string;
  sota: boolean;
}

function shortId(id: string): string {
  return id.length > 12 ? `…${id.slice(-8)}` : id;
}

function ChartTooltip({ active, payload }: { active?: boolean; payload?: Array<{ payload: Point }> }) {
  if (!active || !payload || payload.length === 0) return null;
  const point = payload[0].payload;
  return (
    <div className="chart-tooltip">
      <div className="chart-tooltip__title">{point.name}</div>
      <div className="chart-tooltip__row">
        <span>primary</span>
        <strong>{point.primary.toFixed(4)}</strong>
      </div>
      <div className="chart-tooltip__row">
        <span>status</span>
        <strong>{point.status}</strong>
      </div>
      {point.sota && <div className="chart-tooltip__sota">★ SOTA</div>}
    </div>
  );
}

/** Primary-metric trend across experiments, with the SOTA point highlighted. */
export default function MetricChart() {
  const [points, setPoints] = useState<Point[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const res = await experimentsList();
      setPoints(
        res.experiments
          .filter((d: ExperimentDetail) => d.experiment.eval?.primary != null)
          .map((d: ExperimentDetail) => ({
            name: shortId(d.experiment.id),
            primary: d.experiment.eval!.primary,
            status: d.experiment.status,
            sota: d.sota,
          })),
      );
      setError(null);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return (
    <div className="detail-panel" style={{ display: "flex", flexDirection: "column" }}>
      <div className="panel-toolbar">
        <h3>指标趋势 ({points.length})</h3>
        <button className="btn btn--subtle btn--sm" onClick={() => void refresh()} disabled={loading}>
          {loading ? "加载中…" : "刷新"}
        </button>
      </div>

      {error && <div className="card card--error" style={{ marginBottom: 10 }}>{error}</div>}

      {points.length === 0 ? (
        <EmptyState icon="metrics" message="暂无评估指标。运行实验后此处展示 primary 指标趋势。" />
      ) : (
        <div style={{ width: "100%", height: 280 }}>
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={points} margin={{ top: 12, right: 16, bottom: 4, left: 0 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" vertical={false} />
              <XAxis
                dataKey="name"
                tick={{ fontSize: 11, fill: "var(--text-secondary)" }}
                tickLine={false}
                axisLine={{ stroke: "var(--border)" }}
              />
              <YAxis
                tick={{ fontSize: 11, fill: "var(--text-secondary)" }}
                tickLine={false}
                axisLine={false}
                width={52}
                domain={["auto", "auto"]}
              />
              <Tooltip content={<ChartTooltip />} cursor={{ stroke: "var(--border-strong)" }} />
              <Line
                type="monotone"
                dataKey="primary"
                stroke="var(--accent)"
                strokeWidth={2}
                isAnimationActive={false}
                dot={({ cx, cy, payload }: { cx?: number; cy?: number; payload?: Point }) => {
                  if (cx == null || cy == null || !payload) return <></>;
                  const sota = payload.sota;
                  return (
                    <circle
                      cx={cx}
                      cy={cy}
                      r={sota ? 6 : 3}
                      fill={sota ? "var(--success)" : "var(--accent)"}
                      stroke="var(--surface)"
                      strokeWidth={sota ? 2 : 1}
                    />
                  );
                }}
                activeDot={{ r: 5 }}
              />
            </LineChart>
          </ResponsiveContainer>
        </div>
      )}
    </div>
  );
}
