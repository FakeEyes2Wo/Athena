import { useCallback, useEffect, useState } from "react";
import { errorMessage } from "../lib/errors";
import { experimentsList, generateReport, type ExperimentDetail } from "../lib/tauri-bridge";
import { EmptyState } from "./common/EmptyState";
import styles from "./ReportPanel.module.css";

interface Summary {
  total: number;
  succeeded: number;
  failed: number;
  running: number;
  pending: number;
  bestPrimary: number | null;
  sotaId: string | null;
  sotaPrimary: number | null;
  hypothesisCount: number;
}

function summarize(experiments: ExperimentDetail[]): Summary {
  const summary: Summary = {
    total: experiments.length,
    succeeded: 0,
    failed: 0,
    running: 0,
    pending: 0,
    bestPrimary: null,
    sotaId: null,
    sotaPrimary: null,
    hypothesisCount: experiments.length,
  };
  for (const d of experiments) {
    const status = d.experiment.status;
    if (status === "SUCCEEDED") summary.succeeded += 1;
    else if (status === "FAILED") summary.failed += 1;
    else if (status === "RUNNING") summary.running += 1;
    else if (status === "PENDING") summary.pending += 1;

    const primary = d.experiment.eval?.primary ?? null;
    if (primary != null && (summary.bestPrimary == null || primary > summary.bestPrimary)) {
      summary.bestPrimary = primary;
    }
    if (d.sota) {
      summary.sotaId = d.experiment.id;
      summary.sotaPrimary = primary;
    }
  }
  return summary;
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className={styles["report-stat"]}>
      <div className={styles["report-stat__label"]}>{label}</div>
      <div className={styles["report-stat__value"]}>{value}</div>
    </div>
  );
}

/** Research summary report: SOTA, best metric, and experiment status breakdown. */
export default function ReportPanel() {
  const [summary, setSummary] = useState<Summary | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [report, setReport] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const res = await experimentsList();
      setSummary(summarize(res.experiments));
      setError(null);
    } catch (err) {
      setError(errorMessage(err));
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  async function handleGenerate() {
    try {
      const result = (await generateReport()) as { report?: string };
      setReport(result?.report ?? null);
    } catch (err) {
      setError(errorMessage(err));
    }
  }

  return (
    <div className="detail-panel">
      <div className="panel-toolbar">
        <h3>研究摘要</h3>
        <button className="btn btn--subtle btn--sm" onClick={() => void handleGenerate()}>
          生成报告
        </button>
      </div>

      {error && <div className="card card--error" style={{ marginBottom: 10 }}>{error}</div>}
      {report && <pre className="code-block" style={{ marginBottom: 12 }}>{report}</pre>}

      {!summary || summary.total === 0 ? (
        <EmptyState icon="report" message="暂无实验数据。完成实验后此处汇总 SOTA、最佳指标与状态分布。" />
      ) : (
        <div className={styles["report-grid"]}>
          <Stat label="最佳 primary" value={summary.bestPrimary != null ? summary.bestPrimary.toFixed(4) : "—"} />
          <Stat label="SOTA 实验" value={summary.sotaId ?? "—"} />
          <Stat label="SOTA 指标" value={summary.sotaPrimary != null ? summary.sotaPrimary.toFixed(4) : "—"} />
          <Stat label="实验总数" value={String(summary.total)} />
          <Stat label="成功 / 失败" value={`${summary.succeeded} / ${summary.failed}`} />
          <Stat label="进行中 / 待定" value={`${summary.running} / ${summary.pending}`} />
        </div>
      )}
    </div>
  );
}
