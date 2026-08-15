import { useCallback, useEffect, useState } from "react";
import { errorMessage } from "../lib/errors";
import { experimentsList, type ExperimentDetail } from "../lib/tauri-bridge";
import { EmptyState } from "./common/EmptyState";
import { StatusBadge } from "./common/StatusBadge";

function primaryOf(d: ExperimentDetail): string {
  return d.experiment.eval?.primary != null ? d.experiment.eval.primary.toFixed(4) : "—";
}

function winnerOf(d: ExperimentDetail): string {
  return d.experiment.verdict?.winner ?? "—";
}

/** Compact, ordered timeline of experiment lifecycle events. */
export default function ExperimentLog() {
  const [experiments, setExperiments] = useState<ExperimentDetail[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const res = await experimentsList();
      setExperiments(res.experiments);
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
    <div className="detail-panel">
      <div className="panel-toolbar">
        <h3>实验日志 ({experiments.length})</h3>
        <button className="btn btn--subtle btn--sm" onClick={() => void refresh()} disabled={loading}>
          {loading ? "加载中…" : "刷新"}
        </button>
      </div>

      {error && <div className="card card--error" style={{ marginBottom: 10 }}>{error}</div>}

      {experiments.length === 0 ? (
        <EmptyState icon="log" message="暂无实验记录。启动搜索后将在此处按序展示实验生命周期。" />
      ) : (
        <ol className="timeline">
          {experiments.map((d) => (
            <li key={d.experiment.id} className="timeline__item">
              <StatusBadge status={d.experiment.status} />
              <div className="timeline__body">
                <div className="timeline__head">
                  <span className="timeline__id">
                    {d.experiment.id}
                    {d.sota ? " ★" : ""}
                  </span>
                  <span>primary {primaryOf(d)}</span>
                  <span>winner {winnerOf(d)}</span>
                </div>
                <div className="timeline__statement">{d.hypothesis.statement}</div>
              </div>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}
