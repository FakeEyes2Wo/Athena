import { useCallback, useEffect, useState } from "react";
import { errorMessage } from "../lib/errors";
import {
  experimentGet,
  experimentSetSota,
  experimentTransition,
  experimentsList,
  type ExperimentDetail,
} from "../lib/tauri-bridge";
import { EmptyState } from "./common/EmptyState";
import { StatusBadge } from "./common/StatusBadge";

/** Allowed experiment status transitions (see ``research_tree._ALLOWED_TRANSITIONS``). */
const ALLOWED_TRANSITIONS: Record<string, string[]> = {
  PENDING: ["RUNNING", "CANCELLED"],
  RUNNING: ["SUCCEEDED", "FAILED", "CANCELLED"],
  SUCCEEDED: [],
  FAILED: [],
  CANCELLED: [],
};

function primaryOf(detail: ExperimentDetail): string {
  return detail.experiment.eval?.primary != null
    ? detail.experiment.eval.primary.toFixed(4)
    : "--";
}

function winnerOf(detail: ExperimentDetail): string {
  return detail.experiment.verdict?.winner ?? "--";
}

function KV({ label, value }: { label: string; value: string }) {
  return (
    <div className="kv">
      <span className="kv__label">{label}</span>
      <span className="kv__value">{value || "—"}</span>
    </div>
  );
}

/** Experiment table + detail drawer + status transition / SOTA actions. */
export default function ExperimentManager() {
  const [experiments, setExperiments] = useState<ExperimentDetail[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<ExperimentDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [targetStatus, setTargetStatus] = useState<string>("");
  const [transitionError, setTransitionError] = useState<string>("");
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    setRefreshing(true);
    try {
      const res = await experimentsList();
      setExperiments(res.experiments);
      setError(null);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const loadDetail = useCallback(async (id: string) => {
    setLoading(true);
    setTargetStatus("");
    setTransitionError("");
    try {
      const res = await experimentGet(id);
      setDetail(res);
      setError(null);
    } catch (err) {
      setError(errorMessage(err));
      setDetail(null);
    } finally {
      setLoading(false);
    }
  }, []);

  function selectRow(id: string) {
    setSelectedId(id);
    void loadDetail(id);
  }

  async function applyTransition() {
    if (!detail || !targetStatus) return;
    if (targetStatus === "FAILED" && !transitionError.trim()) {
      setError("FAILED 状态需要填写 error 说明。");
      return;
    }
    setBusy(true);
    try {
      await experimentTransition(
        detail.experiment.id,
        targetStatus,
        targetStatus === "FAILED" ? transitionError.trim() : undefined,
      );
      await refresh();
      await loadDetail(detail.experiment.id);
      setError(null);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  async function markSota() {
    if (!detail) return;
    setBusy(true);
    try {
      await experimentSetSota(detail.experiment.id);
      await refresh();
      await loadDetail(detail.experiment.id);
      setError(null);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  const allowedTargets = detail ? ALLOWED_TRANSITIONS[detail.experiment.status] ?? [] : [];
  const canSetSota =
    !!detail &&
    detail.experiment.status === "SUCCEEDED" &&
    ["baseline", "search"].includes(detail.experiment.plan.kind) &&
    !detail.sota;

  return (
    <div className="detail-panel">
      <div className="panel-toolbar">
        <h3>实验管理 ({experiments.length})</h3>
        <button className="btn btn--subtle btn--sm" onClick={() => void refresh()} disabled={refreshing}>
          {refreshing ? "刷新中…" : "刷新"}
        </button>
      </div>

      {error && <div className="card card--error" style={{ marginBottom: 10 }}>{error}</div>}

      {experiments.length === 0 ? (
        <EmptyState icon="experiments" message="暂无实验。启动搜索后将在此处管理实验状态。" />
      ) : (
        <table className="table">
          <thead>
            <tr>
              <th>实验</th>
              <th>状态</th>
              <th>假设</th>
              <th>primary</th>
              <th>winner</th>
            </tr>
          </thead>
          <tbody>
            {experiments.map((exp) => (
              <tr
                key={exp.experiment.id}
                onClick={() => selectRow(exp.experiment.id)}
                className={exp.experiment.id === selectedId ? "is-selected" : ""}
                style={{ cursor: "pointer" }}
              >
                <td style={{ fontFamily: "var(--font-mono)", fontSize: 12 }}>
                  {exp.experiment.id}
                  {exp.sota ? " ★" : ""}
                </td>
                <td><StatusBadge status={exp.experiment.status} /></td>
                <td style={{ maxWidth: 320 }}>
                  <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", display: "block" }}>
                    {exp.hypothesis.statement}
                  </span>
                </td>
                <td>{primaryOf(exp)}</td>
                <td>{winnerOf(exp)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {selectedId && (
        <div className="card" style={{ marginTop: 12 }}>
          <div className="panel-toolbar">
            <h3 style={{ fontSize: 14 }}>实验详情</h3>
            <button
              className="btn btn--ghost btn--sm"
              onClick={() => {
                setSelectedId(null);
                setDetail(null);
                setTargetStatus("");
                setTransitionError("");
              }}
            >
              关闭
            </button>
          </div>

          {loading && <p style={{ color: "var(--text-secondary)" }}>加载详情中…</p>}

          {detail && (
            <>
              <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr) minmax(0, 1fr)", gap: "6px 24px", marginBottom: 12 }}>
                <KV label="实验 ID" value={detail.experiment.id} />
                <KV label="状态" value={detail.experiment.status} />
                <KV label="父实验" value={detail.experiment.parent_id ?? "—"} />
                <KV label="假设 ID" value={detail.experiment.hypothesis_id} />
                <KV label="plan.kind" value={detail.experiment.plan.kind} />
                <KV label="commit" value={detail.experiment.commit} />
                <KV label="SOTA" value={detail.sota ? "是" : "否"} />
                <KV label="error" value={detail.experiment.error ?? "—"} />
                {detail.experiment.eval && (
                  <KV label="eval.primary" value={detail.experiment.eval.primary.toFixed(4)} />
                )}
                {detail.experiment.verdict && (
                  <KV
                    label="verdict"
                    value={`${detail.experiment.verdict.winner} (p=${detail.experiment.verdict.p_value})`}
                  />
                )}
              </div>

              <div style={{ marginBottom: 12 }}>
                <div style={{ fontWeight: 600, fontSize: 12, color: "var(--text-secondary)", marginBottom: 4 }}>
                  假设陈述
                </div>
                <p style={{ margin: 0, fontSize: 13 }}>{detail.hypothesis.statement}</p>
                <p style={{ margin: "4px 0 0", fontSize: 12, color: "var(--text-secondary)" }}>
                  干预: {detail.hypothesis.intervention} · 预期: {detail.hypothesis.expected_effect}
                </p>
              </div>

              {detail.path && detail.path.length > 0 && (
                <div style={{ marginBottom: 8, fontSize: 12, color: "var(--text-secondary)" }}>
                  祖先链: {detail.path.join(" → ")}
                </div>
              )}
              {detail.descendants && detail.descendants.length > 0 && (
                <div style={{ marginBottom: 8, fontSize: 12, color: "var(--text-secondary)" }}>
                  后代: {detail.descendants.join(", ")}
                </div>
              )}

              <div
                style={{
                  display: "flex",
                  gap: 8,
                  alignItems: "center",
                  flexWrap: "wrap",
                  borderTop: "1px solid var(--border)",
                  paddingTop: 12,
                }}
              >
                <select
                  className="select"
                  style={{ width: "auto", minWidth: 160 }}
                  value={targetStatus}
                  onChange={(e) => setTargetStatus(e.target.value)}
                >
                  <option value="">推进状态…</option>
                  {allowedTargets.map((target) => (
                    <option key={target} value={target}>{target}</option>
                  ))}
                </select>

                {targetStatus === "FAILED" && (
                  <input
                    className="input"
                    style={{ minWidth: 200 }}
                    type="text"
                    value={transitionError}
                    onChange={(e) => setTransitionError(e.target.value)}
                    placeholder="error 说明（必填）"
                  />
                )}

                <button
                  className="btn btn--subtle btn--sm"
                  onClick={() => void applyTransition()}
                  disabled={busy || !targetStatus || allowedTargets.length === 0}
                >
                  确认推进
                </button>

                <button
                  className="btn btn--primary btn--sm"
                  onClick={() => void markSota()}
                  disabled={busy || !canSetSota}
                >
                  {detail.sota ? "当前 SOTA" : "设为 SOTA"}
                </button>
              </div>
            </>
          )}
        </div>
      )}
    </div>
  );
}
