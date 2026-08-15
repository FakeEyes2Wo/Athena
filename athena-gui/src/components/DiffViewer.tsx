import { useCallback, useEffect, useState } from "react";
import { errorMessage } from "../lib/errors";
import { experimentsList, type ExperimentDetail } from "../lib/tauri-bridge";
import { EmptyState } from "./common/EmptyState";

/** Best experiment's change spec: what changed, acceptance rule, and rubrics. */
export default function DiffViewer() {
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

  const plan = detail?.experiment.plan;

  return (
    <div className="detail-panel">
      <div className="panel-toolbar">
        <h3>变更详情</h3>
        <button className="btn btn--subtle btn--sm" onClick={() => void refresh()}>刷新</button>
      </div>

      {error && <div className="card card--error" style={{ marginBottom: 10 }}>{error}</div>}

      {!detail || !plan ? (
        <EmptyState icon="diff" message="暂无实验。运行搜索后此处展示最佳实验的变更规格。" />
      ) : (
        <>
          <div className="card">
            <h4 className="card__title">实验</h4>
            <p className="card__body" style={{ fontFamily: "var(--font-mono)", fontSize: 13 }}>
              {detail.experiment.id}
              {detail.sota ? " ★ SOTA" : ""}
            </p>

            <h4 className="card__title">变更内容</h4>
            <p className="card__body">{plan.change || "—"}</p>

            <h4 className="card__title">验收标准</h4>
            <p className="card__body">{plan.acceptance_rule || "—"}</p>

            {plan.rubrics.length > 0 && (
              <>
                <h4 className="card__title">评价指标</h4>
                <ul style={{ margin: 0, paddingLeft: 18, fontSize: 13 }}>
                  {plan.rubrics.map((rubric) => (
                    <li key={rubric}>{rubric}</li>
                  ))}
                </ul>
              </>
            )}
          </div>

          <div className="card">
            <h4 className="card__title">工作区</h4>
            <div className="kv"><span className="kv__label">路径</span><span className="kv__value">{detail.experiment.gitwork.path}</span></div>
            <div className="kv"><span className="kv__label">分支</span><span className="kv__value">{detail.experiment.gitwork.branch}</span></div>
            <div className="kv"><span className="kv__label">基线提交</span><span className="kv__value">{detail.experiment.gitwork.base_commit}</span></div>
          </div>
        </>
      )}
    </div>
  );
}
