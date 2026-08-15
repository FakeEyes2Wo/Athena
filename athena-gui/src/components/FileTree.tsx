import { useCallback, useEffect, useState } from "react";
import { errorMessage } from "../lib/errors";
import { experimentsList, type ExperimentDetail } from "../lib/tauri-bridge";
import { EmptyState } from "./common/EmptyState";

/** Best experiment's workspace and produced artifacts. */
export default function FileTree() {
  const [detail, setDetail] = useState<ExperimentDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const res = await experimentsList();
      const sota = res.experiments.find((d) => d.sota);
      const target = sota ?? res.experiments[res.experiments.length - 1] ?? null;
      setDetail(target);
      setError(null);
    } catch (err) {
      setError(errorMessage(err));
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const artifacts = detail ? Object.entries(detail.experiment.artifacts) : [];

  return (
    <div className="detail-panel">
      <div className="panel-toolbar">
        <h3>工作区与产物</h3>
        <button className="btn btn--subtle btn--sm" onClick={() => void refresh()}>刷新</button>
      </div>

      {error && <div className="card card--error" style={{ marginBottom: 10 }}>{error}</div>}

      {!detail ? (
        <EmptyState icon="files" message="暂无实验。运行搜索后此处展示最佳实验的工作区与产物。" />
      ) : (
        <>
          <div className="card">
            <h4 className="card__title">工作区</h4>
            <div className="kv"><span className="kv__label">路径</span><span className="kv__value">{detail.experiment.gitwork.path}</span></div>
            <div className="kv"><span className="kv__label">分支</span><span className="kv__value">{detail.experiment.gitwork.branch}</span></div>
            <div className="kv"><span className="kv__label">基线提交</span><span className="kv__value">{detail.experiment.gitwork.base_commit}</span></div>
          </div>

          <div className="card">
            <h4 className="card__title">产物 ({artifacts.length})</h4>
            {artifacts.length === 0 ? (
              <p className="card__body" style={{ color: "var(--text-tertiary)" }}>暂无产物引用。</p>
            ) : (
              <ul style={{ margin: 0, paddingLeft: 0, listStyle: "none" }}>
                {artifacts.map(([kind, ref]) => (
                  <li key={kind} className="kv">
                    <span className="kv__label">{kind}</span>
                    <span className="kv__value" style={{ fontFamily: "var(--font-mono)", fontSize: 12 }}>{ref}</span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </>
      )}
    </div>
  );
}
