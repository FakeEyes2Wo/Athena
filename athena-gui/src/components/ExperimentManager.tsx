import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import { errorMessage } from "../lib/errors";
import { formatMetric } from "../lib/labels";
import {
  experimentGet,
  experimentSetSota,
  experimentTransition,
  experimentsList,
  type ExperimentDetail,
} from "../lib/tauri-bridge";
import { useRemoteData } from "../hooks/useRemoteData";
import { useResearchSignal } from "../hooks/useResearchSignal";
import { EmptyState } from "./common/EmptyState";
import { Icon } from "./common/Icon";
import { EXPERIMENT_STATUS, StatusBadge } from "./common/StatusBadge";
import { useConfirm } from "./common/useConfirm";
import styles from "./ExperimentManager.module.css";

/** Allowed experiment status transitions (see ``research_tree._ALLOWED_TRANSITIONS``). */
const ALLOWED_TRANSITIONS: Record<string, string[]> = {
  PENDING: ["RUNNING", "CANCELLED"],
  RUNNING: ["SUCCEEDED", "FAILED", "CANCELLED"],
  SUCCEEDED: [],
  FAILED: [],
  CANCELLED: [],
};

const KIND_LABEL: Record<string, string> = {
  baseline: "基线",
  search: "搜索",
  validate: "验证",
  final: "最终",
  eda: "数据分析",
};

const VERDICT_LABEL: Record<string, { label: string; tone: string }> = {
  candidate: { label: "优于基线", tone: "success" },
  baseline: { label: "未超过基线", tone: "neutral" },
  tie: { label: "持平", tone: "neutral" },
};

type Filter = "all" | "SUCCEEDED" | "FAILED" | "active";

function pValue(p: number): string {
  return p < 0.001 ? "< 0.001" : p.toFixed(3);
}

function Verdict({ detail }: { detail: ExperimentDetail }) {
  const verdict = detail.experiment.verdict;
  if (!verdict) return <span className={styles.muted}>—</span>;
  const meta = VERDICT_LABEL[verdict.winner] ?? { label: verdict.winner, tone: "neutral" };
  return <span className={`badge badge--${meta.tone}`}>{meta.label}</span>;
}

/** Experiment table with filters and a detail panel (result, change, workspace, lineage). */
export default function ExperimentManager() {
  const { data, error, loading, refresh } = useRemoteData(() => experimentsList().then((res) => res.experiments));
  const experiments = useMemo(() => data ?? [], [data]);
  const [filter, setFilter] = useState<Filter>("all");
  const [query, setQuery] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const counts = useMemo(() => {
    const result = { all: experiments.length, SUCCEEDED: 0, FAILED: 0, active: 0 };
    for (const item of experiments) {
      const status = item.experiment.status;
      if (status === "SUCCEEDED") result.SUCCEEDED += 1;
      else if (status === "FAILED") result.FAILED += 1;
      else if (status === "RUNNING" || status === "PENDING") result.active += 1;
    }
    return result;
  }, [experiments]);

  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return experiments.filter((item) => {
      const status = item.experiment.status;
      const passes =
        filter === "all" ||
        (filter === "active" ? status === "RUNNING" || status === "PENDING" : status === filter);
      if (!passes) return false;
      if (!needle) return true;
      return (
        item.experiment.id.toLowerCase().includes(needle) ||
        item.hypothesis.statement.toLowerCase().includes(needle)
      );
    });
  }, [experiments, filter, query]);

  return (
    <div className={styles.layout}>
      <div className={styles.listPane}>
        <div className="panel-toolbar">
          <div className="segmented" role="group" aria-label="按状态筛选">
            {(
              [
                ["all", "全部"],
                ["SUCCEEDED", "成功"],
                ["FAILED", "失败"],
                ["active", "进行中"],
              ] as Array<[Filter, string]>
            ).map(([key, label]) => (
              <button key={key} type="button" aria-pressed={filter === key} onClick={() => setFilter(key)}>
                {label}
                <span className={styles.count}>{counts[key]}</span>
              </button>
            ))}
          </div>
          <div className="panel-toolbar__actions">
            <div className={styles.search}>
              <Icon name="search" size={14} />
              <input
                type="search"
                placeholder="搜索实验或假设"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                aria-label="搜索实验或假设"
              />
            </div>
            <button
              type="button"
              className="icon-btn"
              onClick={() => void refresh()}
              disabled={loading}
              aria-label="刷新"
              title="刷新"
            >
              <Icon name="refresh" size={15} className={loading ? "icon--spin" : undefined} />
            </button>
          </div>
        </div>

        {error && (
          <div className="callout callout--error" role="alert">
            <Icon name="alert" size={15} />
            <div className="callout__body">{error}</div>
          </div>
        )}

        {experiments.length === 0 ? (
          loading ? (
            <EmptyState icon="loader" message="正在加载实验…" />
          ) : (
            <EmptyState
              icon="experiments"
              title="还没有实验"
              message="确认任务并开始研究后，基线与每次搜索尝试都会出现在这里。"
            />
          )
        ) : visible.length === 0 ? (
          <EmptyState icon="search" message="没有符合筛选条件的实验。" />
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>实验</th>
                  <th>状态</th>
                  <th>类型</th>
                  <th>假设</th>
                  <th className="num">主指标</th>
                  <th>结论</th>
                </tr>
              </thead>
              <tbody>
                {visible.map((item) => (
                  <tr
                    key={item.experiment.id}
                    data-clickable="true"
                    className={item.experiment.id === selectedId ? "is-selected" : ""}
                    onClick={() => setSelectedId(item.experiment.id)}
                    tabIndex={0}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" || e.key === " ") {
                        e.preventDefault();
                        setSelectedId(item.experiment.id);
                      }
                    }}
                    aria-selected={item.experiment.id === selectedId}
                  >
                    <td className="mono">
                      <span className={styles.idCell}>
                        {item.sota && (
                          <span className={styles.sota} title="当前 SOTA">
                            <Icon name="star" size={12} />
                          </span>
                        )}
                        {item.experiment.id}
                      </span>
                    </td>
                    <td>
                      <StatusBadge status={item.experiment.status} />
                    </td>
                    <td className={styles.muted}>{KIND_LABEL[item.experiment.plan.kind] ?? item.experiment.plan.kind}</td>
                    <td className={styles.statement}>{item.hypothesis.statement}</td>
                    <td className="num">{formatMetric(item.experiment.eval?.primary)}</td>
                    <td>
                      <Verdict detail={item} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {selectedId && (
        <ExperimentDetailPanel
          key={selectedId}
          experimentId={selectedId}
          onClose={() => setSelectedId(null)}
          onChanged={() => void refresh()}
        />
      )}
    </div>
  );
}

function ExperimentDetailPanel({
  experimentId,
  onClose,
  onChanged,
}: {
  experimentId: string;
  onClose(): void;
  onChanged(): void;
}) {
  const { token, running } = useResearchSignal();
  const [detail, setDetail] = useState<ExperimentDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [targetStatus, setTargetStatus] = useState("");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [confirm, confirmDialog] = useConfirm();

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setDetail(await experimentGet(experimentId));
      setError(null);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setLoading(false);
    }
  }, [experimentId]);

  useEffect(() => {
    void load();
  }, [load, token]);

  async function applyTransition() {
    if (!detail || !targetStatus) return;
    if (targetStatus === "FAILED" && !reason.trim()) {
      setError("标记为失败时需要填写原因。");
      return;
    }
    const label = EXPERIMENT_STATUS[targetStatus]?.label ?? targetStatus;
    const ok = await confirm({
      title: `将实验标记为「${label}」？`,
      message: "这会直接修改研究树中的实验状态，且无法撤销。",
      confirmLabel: "确认修改",
      tone: "danger",
    });
    if (!ok) return;
    setBusy(true);
    try {
      await experimentTransition(detail.experiment.id, targetStatus, targetStatus === "FAILED" ? reason.trim() : undefined);
      setTargetStatus("");
      setReason("");
      onChanged();
      await load();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  async function markSota() {
    if (!detail) return;
    const ok = await confirm({
      title: "设为当前 SOTA？",
      message: "后续搜索会以该实验为最佳基准继续。",
      confirmLabel: "设为 SOTA",
    });
    if (!ok) return;
    setBusy(true);
    try {
      await experimentSetSota(detail.experiment.id);
      onChanged();
      await load();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  const experiment = detail?.experiment;
  const allowedTargets = experiment ? ALLOWED_TRANSITIONS[experiment.status] ?? [] : [];
  const canSetSota =
    !!detail &&
    experiment?.status === "SUCCEEDED" &&
    ["baseline", "search"].includes(experiment.plan.kind) &&
    !detail.sota;
  const secondary = experiment?.eval ? Object.entries(experiment.eval.secondary ?? {}) : [];
  const artifacts = experiment ? Object.entries(experiment.artifacts ?? {}) : [];

  return (
    <aside className={styles.detail} aria-label="实验详情">
      <header className={styles.detailHeader}>
        <div className={styles.detailTitle}>
          <span className={styles.detailKicker}>实验详情</span>
          <span className={styles.detailId}>{experimentId}</span>
        </div>
        <button type="button" className="icon-btn" onClick={onClose} aria-label="关闭详情">
          <Icon name="close" size={16} />
        </button>
      </header>

      <div className={styles.detailBody}>
        {error && (
          <div className="callout callout--error" role="alert">
            <Icon name="alert" size={15} />
            <div className="callout__body">{error}</div>
          </div>
        )}
        {loading && !detail && <EmptyState icon="loader" message="加载详情中…" />}

        {detail && experiment && (
          <>
            <div className={styles.badges}>
              <StatusBadge status={experiment.status} />
              <span className="badge badge--neutral">{KIND_LABEL[experiment.plan.kind] ?? experiment.plan.kind}</span>
              {detail.sota && (
                <span className="badge badge--gold">
                  <Icon name="star" size={11} /> SOTA
                </span>
              )}
            </div>

            <Block title="假设">
              <p className={styles.statementFull}>{detail.hypothesis.statement}</p>
              <dl className={styles.dl}>
                <dt>干预</dt>
                <dd>{detail.hypothesis.intervention || "—"}</dd>
                <dt>预期效果</dt>
                <dd>{detail.hypothesis.expected_effect || "—"}</dd>
              </dl>
            </Block>

            <Block title="结果">
              <div className="stat-grid" style={{ gridTemplateColumns: "1fr 1fr" }}>
                <div className="stat">
                  <div className="stat__label">主指标</div>
                  <div className="stat__value" style={{ color: detail.sota ? "var(--gold)" : undefined }}>
                    {formatMetric(experiment.eval?.primary)}
                  </div>
                </div>
                <div className="stat">
                  <div className="stat__label">与基线比较</div>
                  <div className={styles.verdict}>
                    <Verdict detail={detail} />
                    {experiment.verdict && <span className={styles.muted}>p = {pValue(experiment.verdict.p_value)}</span>}
                  </div>
                </div>
              </div>
              {secondary.length > 0 && (
                <dl className={styles.dl}>
                  {secondary.map(([name, value]) => (
                    <div key={name} className={styles.dlRow}>
                      <dt>{name}</dt>
                      <dd className={styles.mono}>{typeof value === "number" ? formatMetric(value) : String(value)}</dd>
                    </div>
                  ))}
                </dl>
              )}
              {experiment.error && (
                <div className="callout callout--error">
                  <Icon name="alert" size={15} />
                  <div className="callout__body" style={{ whiteSpace: "pre-wrap" }}>
                    {experiment.error}
                  </div>
                </div>
              )}
            </Block>

            <Block title="变更内容">
              <p className={styles.text}>{experiment.plan.change || "—"}</p>
              <dl className={styles.dl}>
                <dt>验收标准</dt>
                <dd>{experiment.plan.acceptance_rule || "—"}</dd>
              </dl>
              {experiment.plan.rubrics.length > 0 && (
                <ul className={styles.rubrics}>
                  {experiment.plan.rubrics.map((rubric) => (
                    <li key={rubric}>{rubric}</li>
                  ))}
                </ul>
              )}
            </Block>

            <Block title="工作区与产物">
              <dl className={styles.dl}>
                <dt>路径</dt>
                <dd className={styles.mono}>{experiment.gitwork.path || "—"}</dd>
                <dt>分支</dt>
                <dd className={styles.mono}>{experiment.gitwork.branch || "—"}</dd>
                <dt>提交</dt>
                <dd className={styles.mono}>{experiment.commit ? experiment.commit.slice(0, 12) : "—"}</dd>
                <dt>基线提交</dt>
                <dd className={styles.mono}>{experiment.gitwork.base_commit ? experiment.gitwork.base_commit.slice(0, 12) : "—"}</dd>
              </dl>
              {artifacts.length > 0 && (
                <ul className={styles.artifacts}>
                  {artifacts.map(([kind, ref]) => (
                    <li key={kind}>
                      <span className={styles.artifactKind}>{kind}</span>
                      <span className={styles.mono}>{ref}</span>
                    </li>
                  ))}
                </ul>
              )}
            </Block>

            {((detail.path?.length ?? 0) > 0 || (detail.descendants?.length ?? 0) > 0) && (
              <Block title="谱系">
                {detail.path && detail.path.length > 0 && (
                  <div className={styles.lineage}>
                    {detail.path.map((id, index) => (
                      <span key={id} className={styles.lineageItem}>
                        {index > 0 && <Icon name="chevronRight" size={12} />}
                        <span className={id === experimentId ? styles.lineageCurrent : undefined}>{id}</span>
                      </span>
                    ))}
                  </div>
                )}
                {detail.descendants && detail.descendants.length > 0 && (
                  <p className={styles.mutedSmall}>后代实验：{detail.descendants.join("、")}</p>
                )}
              </Block>
            )}

            <details className={styles.manage}>
              <summary>
                <Icon name="wrench" size={14} /> 人工调整
              </summary>
              <p className={styles.mutedSmall}>
                这些操作会直接修改研究树，仅用于纠正异常状态。{running ? "研究运行期间不可用。" : ""}
              </p>
              <div className={styles.manageRow}>
                <select
                  className="select"
                  value={targetStatus}
                  onChange={(e) => setTargetStatus(e.target.value)}
                  disabled={running || allowedTargets.length === 0}
                  aria-label="目标状态"
                >
                  <option value="">{allowedTargets.length === 0 ? "当前状态不可变更" : "变更状态为…"}</option>
                  {allowedTargets.map((target) => (
                    <option key={target} value={target}>
                      {EXPERIMENT_STATUS[target]?.label ?? target}
                    </option>
                  ))}
                </select>
                <button
                  type="button"
                  className="btn btn--sm"
                  onClick={() => void applyTransition()}
                  disabled={running || busy || !targetStatus}
                >
                  应用
                </button>
              </div>
              {targetStatus === "FAILED" && (
                <input
                  className="input"
                  type="text"
                  value={reason}
                  onChange={(e) => setReason(e.target.value)}
                  placeholder="失败原因（必填）"
                  aria-label="失败原因"
                />
              )}
              <button
                type="button"
                className="btn btn--sm"
                onClick={() => void markSota()}
                disabled={running || busy || !canSetSota}
                title={detail.sota ? "已是当前 SOTA" : "仅成功的基线或搜索实验可以设为 SOTA"}
              >
                <Icon name="star" size={13} /> {detail.sota ? "已是当前 SOTA" : "设为 SOTA"}
              </button>
            </details>
          </>
        )}
      </div>
      {confirmDialog}
    </aside>
  );
}

function Block({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className={styles.block}>
      <h4 className="section-label">{title}</h4>
      {children}
    </section>
  );
}
