import { Suspense, lazy, useState } from "react";
import type { ClarificationPreview, ModuleKey, PipelineViewModel } from "../../types/ui";
import {
  DIRECTION_LABEL,
  formatMetric,
  phaseLabel,
  statusLabel,
  taskTypeLabel,
} from "../../lib/labels";
import { EmptyState } from "../common/EmptyState";
import { ErrorBoundary } from "../common/ErrorBoundary";
import { Icon } from "../common/Icon";
import styles from "./Inspector.module.css";

const MetricChart = lazy(() => import("../MetricChart"));
const ExperimentLog = lazy(() => import("../ExperimentLog"));

type InspectorTab = "overview" | "metrics" | "experiments";

const TABS: Array<{ key: InspectorTab; label: string }> = [
  { key: "overview", label: "概览" },
  { key: "metrics", label: "指标" },
  { key: "experiments", label: "实验记录" },
];

interface InspectorProps {
  viewModel: PipelineViewModel;
  draft: ClarificationPreview | null;
  onClose(): void;
  onOpenModule(module: ModuleKey): void;
}

/** Right-hand research inspector for the conversation: progress, metrics, experiments. */
export function Inspector({ viewModel, draft, onClose, onOpenModule }: InspectorProps) {
  const [tab, setTab] = useState<InspectorTab>("overview");

  return (
    <aside className={styles.inspector} aria-label="研究概览">
      <header className={styles.header}>
        <div className="tabs" role="tablist" aria-label="研究概览视图" style={{ border: "none" }}>
          {TABS.map((item) => (
            <button
              key={item.key}
              type="button"
              role="tab"
              className="tab"
              aria-selected={tab === item.key}
              onClick={() => setTab(item.key)}
            >
              {item.label}
            </button>
          ))}
        </div>
        <button type="button" className="icon-btn" onClick={onClose} aria-label="关闭研究概览">
          <Icon name="close" size={16} />
        </button>
      </header>
      <div className={styles.body}>
        {tab === "overview" ? (
          <Overview viewModel={viewModel} draft={draft} onOpenModule={onOpenModule} />
        ) : (
          <Suspense fallback={<EmptyState icon="loader" message="加载中…" />}>
            <ErrorBoundary>{tab === "metrics" ? <MetricChart compact /> : <ExperimentLog />}</ErrorBoundary>
          </Suspense>
        )}
      </div>
    </aside>
  );
}

function Overview({
  viewModel,
  draft,
  onOpenModule,
}: {
  viewModel: PipelineViewModel;
  draft: ClarificationPreview | null;
  onOpenModule(module: ModuleKey): void;
}) {
  const { rightRail, phase, status } = viewModel;
  const limit = rightRail.searchLimit;
  const attempts = rightRail.searchAttempts;
  const progress = limit > 0 ? Math.min(100, Math.round((attempts / limit) * 100)) : 0;
  const understanding = draft?.understanding;

  return (
    <div className={styles.overview}>
      <section className={styles.section}>
        <div className={styles.phaseRow}>
          <span className={styles.phase} data-status={status}>
            <span className={styles.phaseDot} />
            {phaseLabel(phase)} · {statusLabel(status)}
          </span>
          <span className={styles.mode}>{viewModel.manual ? "手动选假设" : "自动选假设"}</span>
        </div>
        <div className={styles.progressLabel}>
          <span>搜索进度</span>
          <span className={styles.mono}>
            {attempts}
            {limit > 0 ? ` / ${limit}` : ""}
          </span>
        </div>
        <div
          className={styles.progress}
          role="progressbar"
          aria-label="搜索进度"
          aria-valuemin={0}
          aria-valuemax={limit || undefined}
          aria-valuenow={attempts}
        >
          <span style={{ width: `${progress}%` }} />
        </div>
      </section>

      <section className="stat-grid" style={{ gridTemplateColumns: "1fr 1fr" }}>
        <div className="stat">
          <div className="stat__label">
            <Icon name="trophy" size={13} /> 最佳指标
          </div>
          <div className="stat__value" style={{ color: rightRail.bestPrimary != null ? "var(--gold)" : undefined }}>
            {formatMetric(rightRail.bestPrimary)}
          </div>
        </div>
        <div className="stat">
          <div className="stat__label">
            <Icon name="success" size={13} /> 成功实验
          </div>
          <div className="stat__value">{rightRail.successes}</div>
        </div>
        <div className="stat">
          <div className="stat__label">
            <Icon name="wallet" size={13} /> 剩余预算
          </div>
          <div className="stat__value">{limit > 0 ? rightRail.budgetRemaining : "—"}</div>
        </div>
        <div className="stat">
          <div className="stat__label">
            <Icon name="cpu" size={13} /> 并发
          </div>
          <div className="stat__value">{rightRail.workers || "—"}</div>
        </div>
      </section>

      {rightRail.latestExperimentId && (
        <section className={styles.section}>
          <h3 className="section-label">当前最佳实验</h3>
          <button
            type="button"
            className={styles.linkRow}
            onClick={() => onOpenModule("experiments")}
            title="在实验页查看"
          >
            <Icon name="star" size={14} />
            <span className={styles.mono}>{rightRail.latestExperimentId}</span>
            <Icon name="arrowRight" size={14} />
          </button>
        </section>
      )}

      {understanding && understanding.title && (
        <section className={styles.section}>
          <h3 className="section-label">任务</h3>
          <div className={styles.task}>
            <p className={styles.taskTitle}>{understanding.title}</p>
            <dl className={styles.taskFields}>
              <dt>数据集</dt>
              <dd>{understanding.dataset || "—"}</dd>
              <dt>目标</dt>
              <dd>{understanding.target || "—"}</dd>
              <dt>类型</dt>
              <dd>{taskTypeLabel(understanding.task_type) ?? "—"}</dd>
              <dt>主指标</dt>
              <dd>
                {understanding.primary_metric ?? "—"}
                {understanding.direction ? ` · ${DIRECTION_LABEL[understanding.direction] ?? understanding.direction}` : ""}
              </dd>
            </dl>
          </div>
        </section>
      )}

      <section className={styles.section}>
        <h3 className="section-label">深入查看</h3>
        <div className={styles.links}>
          <button type="button" className="btn btn--sm" onClick={() => onOpenModule("research-tree")}>
            <Icon name="tree" size={14} /> 研究树
          </button>
          <button type="button" className="btn btn--sm" onClick={() => onOpenModule("experiments")}>
            <Icon name="experiments" size={14} /> 实验
          </button>
          <button type="button" className="btn btn--sm" onClick={() => onOpenModule("report")}>
            <Icon name="report" size={14} /> 报告
          </button>
        </div>
      </section>
    </div>
  );
}
