import { useMemo, useState } from "react";
import { errorMessage } from "../lib/errors";
import { formatMetric } from "../lib/labels";
import { experimentsList, generateReport, settingsGet, type ExperimentDetail } from "../lib/tauri-bridge";
import { useRemoteData } from "../hooks/useRemoteData";
import { EmptyState } from "./common/EmptyState";
import { Icon } from "./common/Icon";
import { Markdown } from "./common/Markdown";
import styles from "./ReportPanel.module.css";

interface Summary {
  total: number;
  succeeded: number;
  failed: number;
  active: number;
  best: number | null;
  sotaId: string | null;
  sotaPrimary: number | null;
}

function summarize(experiments: ExperimentDetail[], minimize: boolean): Summary {
  const summary: Summary = {
    total: experiments.length,
    succeeded: 0,
    failed: 0,
    active: 0,
    best: null,
    sotaId: null,
    sotaPrimary: null,
  };
  for (const item of experiments) {
    const status = item.experiment.status;
    if (status === "SUCCEEDED") summary.succeeded += 1;
    else if (status === "FAILED") summary.failed += 1;
    else if (status === "RUNNING" || status === "PENDING") summary.active += 1;
    const primary = item.experiment.eval?.primary ?? null;
    if (primary != null && (summary.best == null || (minimize ? primary < summary.best : primary > summary.best))) {
      summary.best = primary;
    }
    if (item.sota) {
      summary.sotaId = item.experiment.id;
      summary.sotaPrimary = primary;
    }
  }
  return summary;
}

/** Research summary and the generated Markdown report (copy / download). */
export default function ReportPanel() {
  const { data, error, loading } = useRemoteData(() => experimentsList().then((res) => res.experiments));
  const settings = useRemoteData(() => settingsGet(), { follow: false });
  const minimize = settings.data?.direction === "minimize";
  const summary = useMemo(() => summarize(data ?? [], minimize), [data, minimize]);
  const [report, setReport] = useState<string | null>(null);
  const [generating, setGenerating] = useState(false);
  const [reportError, setReportError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  async function handleGenerate() {
    setGenerating(true);
    setReportError(null);
    try {
      const result = (await generateReport()) as { report?: string };
      setReport(result?.report ?? "");
    } catch (err) {
      setReportError(errorMessage(err));
    } finally {
      setGenerating(false);
    }
  }

  async function copy() {
    if (!report) return;
    try {
      await navigator.clipboard.writeText(report);
      setCopied(true);
      setTimeout(() => setCopied(false), 1600);
    } catch (err) {
      setReportError(`复制失败：${errorMessage(err)}`);
    }
  }

  function download() {
    if (!report) return;
    const blob = new Blob([report], { type: "text/markdown;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `athena-report-${new Date().toISOString().slice(0, 10)}.md`;
    link.click();
    URL.revokeObjectURL(url);
  }

  return (
    <div className="detail-panel detail-panel--reading" style={{ gap: 20 }}>
      {(error || reportError) && (
        <div className="callout callout--error" role="alert">
          <Icon name="alert" size={15} />
          <div className="callout__body">{reportError ?? error}</div>
        </div>
      )}

      <section className={styles.section}>
        <h3 className="section-label">研究摘要</h3>
        {summary.total === 0 ? (
          <EmptyState
            icon={loading ? "loader" : "report"}
            title={loading ? undefined : "暂无实验数据"}
            message={loading ? "加载中…" : "完成实验后，这里会汇总最佳结果、SOTA 实验与实验状态分布。"}
          />
        ) : (
          <div className="stat-grid">
            <div className="stat">
              <div className="stat__label">
                <Icon name="trophy" size={13} /> 最佳主指标
              </div>
              <div className="stat__value" style={{ color: "var(--gold)" }}>
                {formatMetric(summary.best)}
              </div>
            </div>
            <div className="stat">
              <div className="stat__label">
                <Icon name="star" size={13} /> SOTA 实验
              </div>
              <div className="stat__value stat__value--mono" title={summary.sotaId ?? ""}>
                {summary.sotaId ?? "—"}
              </div>
              <div className="stat__hint">指标 {formatMetric(summary.sotaPrimary)}</div>
            </div>
            <div className="stat">
              <div className="stat__label">实验总数</div>
              <div className="stat__value">{summary.total}</div>
            </div>
            <div className="stat">
              <div className="stat__label">成功 / 失败</div>
              <div className="stat__value">
                {summary.succeeded} <span className={styles.slash}>/</span> {summary.failed}
              </div>
            </div>
            <div className="stat">
              <div className="stat__label">进行中</div>
              <div className="stat__value">{summary.active}</div>
            </div>
          </div>
        )}
      </section>

      <section className={styles.section}>
        <div className="panel-toolbar">
          <h3 className="section-label">研究报告</h3>
          <div className="panel-toolbar__actions">
            {report && (
              <>
                <button type="button" className="btn btn--ghost btn--sm" onClick={() => void copy()}>
                  <Icon name={copied ? "check" : "copy"} size={13} /> {copied ? "已复制" : "复制 Markdown"}
                </button>
                <button type="button" className="btn btn--ghost btn--sm" onClick={download}>
                  <Icon name="download" size={13} /> 下载 .md
                </button>
              </>
            )}
            <button type="button" className="btn btn--primary btn--sm" onClick={() => void handleGenerate()} disabled={generating}>
              {generating ? <Icon name="loader" size={13} className="icon--spin" /> : <Icon name="sparkles" size={13} />}
              {report ? "重新生成" : "生成报告"}
            </button>
          </div>
        </div>
        {report ? (
          <article className={styles.document}>
            <Markdown>{report || "_报告为空_"}</Markdown>
          </article>
        ) : (
          <div className={styles.placeholder}>
            <Icon name="report" size={18} />
            <p>
              根据已保存的研究树与验证结果生成 Markdown 报告。生成过程不会重新运行任何实验或评估。
            </p>
          </div>
        )}
      </section>
    </div>
  );
}
