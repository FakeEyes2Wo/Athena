import { useCallback, useEffect, useState } from "react";
import { Button, Flash, Select, TextInput } from "@primer/react";
import { errorMessage } from "../lib/errors";
import {
  experimentGet,
  experimentSetSota,
  experimentTransition,
  experimentsList,
  type ExperimentDetail,
} from "../lib/tauri-bridge";
import { EmptyState } from "./common/EmptyState";
import { KeyValueList } from "./common/KeyValueList";
import { Panel } from "./common/Panel";
import { Section } from "./common/Section";
import { StatusBadge } from "./common/StatusBadge";
import styles from "./ExperimentManager.module.css";

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

  function closeDetail() {
    setSelectedId(null);
    setDetail(null);
    setTargetStatus("");
    setTransitionError("");
  }

  return (
    <Panel
      title={`实验管理 (${experiments.length})`}
      actions={
        <Button size="small" onClick={() => void refresh()} disabled={refreshing}>
          {refreshing ? "刷新中…" : "刷新"}
        </Button>
      }
    >
      {error && <Flash variant="danger">{error}</Flash>}

      {experiments.length === 0 ? (
        <EmptyState icon="experiments" message="暂无实验。启动搜索后将在此处管理实验状态。" />
      ) : (
        <div className={styles.tableWrap}>
          <table className={styles.table}>
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
                  className={exp.experiment.id === selectedId ? styles.selected : undefined}
                  aria-selected={exp.experiment.id === selectedId}
                >
                  <td className={styles.mono}>
                    {exp.experiment.id}
                    {exp.sota ? " ★" : ""}
                  </td>
                  <td>
                    <StatusBadge status={exp.experiment.status} />
                  </td>
                  <td className={styles.statementCell}>
                    <span className={styles.truncate}>{exp.hypothesis.statement}</span>
                  </td>
                  <td className={styles.mono}>{primaryOf(exp)}</td>
                  <td>{winnerOf(exp)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {selectedId && (
        <Section
          title={
            <span className={styles.detailTitle}>
              实验详情
              <Button size="small" variant="invisible" onClick={closeDetail}>
                关闭
              </Button>
            </span>
          }
        >
          {loading && <p className={styles.muted}>加载详情中…</p>}

          {detail && (
            <>
              <KeyValueList
                columns={2}
                items={[
                  { label: "实验 ID", value: detail.experiment.id, mono: true },
                  { label: "状态", value: detail.experiment.status },
                  { label: "父实验", value: detail.experiment.parent_id ?? "—", mono: true },
                  { label: "假设 ID", value: detail.experiment.hypothesis_id, mono: true },
                  { label: "plan.kind", value: detail.experiment.plan.kind },
                  { label: "commit", value: detail.experiment.commit, mono: true },
                  { label: "SOTA", value: detail.sota ? "是" : "否" },
                  { label: "error", value: detail.experiment.error ?? "—" },
                  ...(detail.experiment.eval
                    ? [{ label: "eval.primary", value: detail.experiment.eval.primary.toFixed(4), mono: true }]
                    : []),
                  ...(detail.experiment.verdict
                    ? [
                        {
                          label: "verdict",
                          value: `${detail.experiment.verdict.winner} (p=${detail.experiment.verdict.p_value})`,
                        },
                      ]
                    : []),
                ]}
              />

              <div className={styles.hypothesis}>
                <div className={styles.subheading}>假设陈述</div>
                <p className={styles.statement}>{detail.hypothesis.statement}</p>
                <p className={styles.muted}>
                  干预: {detail.hypothesis.intervention} · 预期: {detail.hypothesis.expected_effect}
                </p>
              </div>

              {detail.path && detail.path.length > 0 && (
                <p className={styles.muted}>祖先链: {detail.path.join(" → ")}</p>
              )}
              {detail.descendants && detail.descendants.length > 0 && (
                <p className={styles.muted}>后代: {detail.descendants.join(", ")}</p>
              )}

              <div className={styles.actions}>
                <Select
                  aria-label="推进状态"
                  value={targetStatus}
                  onChange={(e) => setTargetStatus(e.target.value)}
                >
                  <Select.Option value="">推进状态…</Select.Option>
                  {allowedTargets.map((target) => (
                    <Select.Option key={target} value={target}>
                      {target}
                    </Select.Option>
                  ))}
                </Select>

                {targetStatus === "FAILED" && (
                  <TextInput
                    aria-label="失败原因"
                    value={transitionError}
                    onChange={(e) => setTransitionError(e.target.value)}
                    placeholder="error 说明（必填）"
                  />
                )}

                <Button
                  size="small"
                  onClick={() => void applyTransition()}
                  disabled={busy || !targetStatus || allowedTargets.length === 0}
                >
                  确认推进
                </Button>

                <Button size="small" variant="primary" onClick={() => void markSota()} disabled={busy || !canSetSota}>
                  {detail.sota ? "当前 SOTA" : "设为 SOTA"}
                </Button>
              </div>
            </>
          )}
        </Section>
      )}
    </Panel>
  );
}
