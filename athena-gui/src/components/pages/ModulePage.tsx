import { Suspense, lazy, useState, type ComponentType, type ReactNode } from "react";
import type { ModuleKey } from "../../types/ui";
import { EmptyState } from "../common/EmptyState";
import { ErrorBoundary } from "../common/ErrorBoundary";
import { Icon } from "../common/Icon";
import { MainHeader } from "../shell/MainHeader";
import { MODULE_BY_KEY } from "../shell/navigation";
import styles from "./ModulePage.module.css";

// Pages are code-split so heavy dependencies (reactflow, recharts, d3-dag,
// react-markdown) only load when their view is first opened.
const ResearchTreeViz = lazy(() => import("../ResearchTreeViz"));
const HypothesisGraphViz = lazy(() => import("../HypothesisGraphViz"));
const AlgorithmsPanel = lazy(() => import("../AlgorithmsPanel"));
const ExperimentManager = lazy(() => import("../ExperimentManager"));
const MetricChart = lazy(() => import("../MetricChart"));
const EDAPreview = lazy(() => import("../EDAPreview"));
const LLMIOPanel = lazy(() => import("../LLMIOPanel"));
const ReportPanel = lazy(() => import("../ReportPanel"));

interface PageTab {
  key: string;
  label: string;
  component: ComponentType;
  /** Graph-like views take the full height instead of scrolling. */
  fill?: boolean;
}

type PageKey = Exclude<ModuleKey, "session">;

const PAGE_TABS: Record<PageKey, PageTab[]> = {
  "research-tree": [
    { key: "lineage", label: "实验谱系", component: ResearchTreeViz, fill: true },
    { key: "hypotheses", label: "假设图", component: HypothesisGraphViz, fill: true },
    { key: "algorithms", label: "图算法", component: AlgorithmsPanel },
  ],
  experiments: [
    { key: "list", label: "实验列表", component: ExperimentManager },
    { key: "metrics", label: "指标趋势", component: MetricChart },
  ],
  eda: [{ key: "eda", label: "数据分析", component: EDAPreview }],
  "llm-io": [{ key: "traces", label: "模型轨迹", component: LLMIOPanel, fill: true }],
  report: [{ key: "report", label: "报告", component: ReportPanel }],
};

interface ModulePageProps {
  module: PageKey;
  /** Global header actions (log toggle, ...). */
  actions?: ReactNode;
  /** A pending human question exists; offer a way back to the conversation. */
  attention?: boolean;
  onOpenConversation(): void;
}

/** One research page: header (title, description, tabs) + lazily loaded content. */
export function ModulePage({ module, actions, attention, onOpenConversation }: ModulePageProps) {
  const def = MODULE_BY_KEY[module];
  const tabs = PAGE_TABS[module];
  const [tabKey, setTabKey] = useState(tabs[0].key);
  const tab = tabs.find((item) => item.key === tabKey) ?? tabs[0];
  const Panel = tab.component;

  return (
    <section className={styles.page} aria-label={def.label}>
      <MainHeader title={def.label} subtitle={def.description} actions={actions} />
      {attention && (
        <div className={styles.attention} role="status">
          <Icon name="question" size={15} />
          <span>Athena 有一个问题正在等待你回答。</span>
          <button type="button" className="btn btn--sm btn--primary" onClick={onOpenConversation}>
            前往对话
          </button>
        </div>
      )}
      {tabs.length > 1 && (
        <div className={styles.tabs}>
          <div className="tabs" role="tablist" aria-label={`${def.label}视图`}>
            {tabs.map((item) => (
              <button
                key={item.key}
                type="button"
                role="tab"
                className="tab"
                aria-selected={item.key === tab.key}
                onClick={() => setTabKey(item.key)}
              >
                {item.label}
              </button>
            ))}
          </div>
        </div>
      )}
      <div className={`${styles.body}${tab.fill ? ` ${styles.fill}` : ""}`} role="tabpanel">
        <Suspense fallback={<EmptyState icon="loader" message="加载中…" />}>
          <ErrorBoundary key={tab.key}>
            <Panel />
          </ErrorBoundary>
        </Suspense>
      </div>
    </section>
  );
}
