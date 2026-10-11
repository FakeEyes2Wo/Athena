import { useState } from "react";
import { edaReport, type EdaFigure } from "../lib/tauri-bridge";
import { useRemoteData } from "../hooks/useRemoteData";
import { Dialog } from "./common/Dialog";
import { EmptyState } from "./common/EmptyState";
import { Icon } from "./common/Icon";
import { Markdown } from "./common/Markdown";
import styles from "./EDAPreview.module.css";

/** Exploratory data analysis report (Markdown with inline figures) and figure gallery. */
export default function EDAPreview() {
  const { data, error, loading, refresh } = useRemoteData(() => edaReport(), { follow: "phase" });
  const [zoomed, setZoomed] = useState<EdaFigure | null>(null);

  return (
    <div className="detail-panel detail-panel--reading" style={{ gap: 16 }}>
      <div className="panel-toolbar">
        <h3>
          {data?.report_name ?? "EDA 报告"}
          {data?.eda_dir && (
            <span className={styles.path} title={data.eda_dir}>
              {data.eda_dir}
            </span>
          )}
        </h3>
        <button type="button" className="btn btn--ghost btn--sm" onClick={() => void refresh()} disabled={loading}>
          <Icon name="refresh" size={13} className={loading ? "icon--spin" : undefined} /> 刷新
        </button>
      </div>

      {error && (
        <div className="callout callout--error" role="alert">
          <Icon name="alert" size={15} />
          <div className="callout__body">{error}</div>
        </div>
      )}

      {!data || !data.eda_dir ? (
        <EmptyState
          icon={loading && !data ? "loader" : "chart"}
          title={loading && !data ? undefined : "暂无数据分析报告"}
          message={
            loading && !data
              ? "加载中…"
              : "研究开始后，PREPARE 阶段会先分析数据集（分布、缺失值、标签平衡等），报告生成后显示在这里。"
          }
        />
      ) : (
        <>
          {data.report ? (
            <article className={styles.document}>
              <Markdown dataImages>{data.report}</Markdown>
            </article>
          ) : (
            <EmptyState icon="report" message="分析目录中还没有 Markdown 报告。" />
          )}

          {data.figures.length > 0 && (
            <section className={styles.gallery}>
              <h4 className="section-label">全部图表（{data.figures.length}）</h4>
              <div className={styles.grid}>
                {data.figures.map((figure) => (
                  <button
                    key={figure.name}
                    type="button"
                    className={styles.figure}
                    onClick={() => setZoomed(figure)}
                    title={`查看大图：${figure.name}`}
                  >
                    <img src={`data:${figure.mime};base64,${figure.data}`} alt={figure.name} loading="lazy" />
                    <span className={styles.caption}>{figure.name}</span>
                  </button>
                ))}
              </div>
            </section>
          )}
        </>
      )}

      <Dialog open={zoomed !== null} title={zoomed?.name ?? ""} onClose={() => setZoomed(null)} size="lg">
        {zoomed && (
          <img
            className={styles.zoomed}
            src={`data:${zoomed.mime};base64,${zoomed.data}`}
            alt={zoomed.name}
          />
        )}
      </Dialog>
    </div>
  );
}
