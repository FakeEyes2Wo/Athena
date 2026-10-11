import { experimentsList } from "../lib/tauri-bridge";
import { formatMetric } from "../lib/labels";
import { useRemoteData } from "../hooks/useRemoteData";
import { EmptyState } from "./common/EmptyState";
import { Icon } from "./common/Icon";
import { StatusBadge } from "./common/StatusBadge";

/** Compact, newest-first list of experiments for the conversation inspector. */
export default function ExperimentLog() {
  const { data, error, loading, refresh } = useRemoteData(() => experimentsList().then((res) => res.experiments));
  const experiments = [...(data ?? [])].reverse();

  return (
    <div className="detail-panel">
      <div className="panel-toolbar">
        <h3>
          实验记录 <span className="panel-toolbar__count">{experiments.length}</span>
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

      {experiments.length === 0 ? (
        <EmptyState
          icon={loading ? "loader" : "experiments"}
          message={loading ? "加载中…" : "暂无实验记录。研究开始后会在这里按时间列出。"}
        />
      ) : (
        <ol style={{ listStyle: "none", margin: 0, padding: 0, display: "flex", flexDirection: "column", gap: 6 }}>
          {experiments.map((item) => (
            <li
              key={item.experiment.id}
              style={{
                padding: "10px 12px",
                border: "1px solid var(--border)",
                borderRadius: "var(--radius-md)",
                background: "var(--surface)",
                display: "flex",
                flexDirection: "column",
                gap: 4,
              }}
            >
              <div style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 12 }}>
                <StatusBadge status={item.experiment.status} />
                <span style={{ fontFamily: "var(--font-mono)", color: "var(--text-secondary)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", flex: 1 }}>
                  {item.experiment.id}
                </span>
                {item.sota && (
                  <span className="badge badge--gold">
                    <Icon name="star" size={10} /> SOTA
                  </span>
                )}
                <span style={{ fontFamily: "var(--font-mono)", fontWeight: 600 }}>{formatMetric(item.experiment.eval?.primary)}</span>
              </div>
              <div style={{ fontSize: 13, lineHeight: 1.5, color: "var(--text-primary)" }}>{item.hypothesis.statement}</div>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}
