import { useCallback, useEffect, useState } from "react";
import { errorMessage } from "../lib/errors";
import ReactMarkdown from "react-markdown";
import { edaReport, type EdaReport } from "../lib/tauri-bridge";
import { EmptyState } from "./common/EmptyState";
import styles from "./EDAPreview.module.css";

/** EDA (探索性数据分析) 报告预览：Markdown 正文 + 图表。 */
export default function EDAPreview() {
  const [data, setData] = useState<EdaReport | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setData(await edaReport());
      setError(null);
    } catch (err) {
      setError(errorMessage(err));
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return (
    <div className="detail-panel">
      <div className="panel-toolbar">
        <h3>EDA 报告</h3>
        <button className="btn btn--subtle btn--sm" onClick={() => void refresh()}>
          刷新
        </button>
      </div>

      {error && <div className="card card--error" style={{ marginBottom: 12 }}>{error}</div>}

      {!data || !data.eda_dir ? (
        <EmptyState icon="files" message="暂无 EDA 报告。运行 PREPARE 阶段后将在此预览数据探索结果。" />
      ) : (
        <div className={styles.body}>
          {data.report ? (
            <article className={styles.markdown}>
              <ReactMarkdown>{data.report}</ReactMarkdown>
            </article>
          ) : (
            <EmptyState icon="report" message="EDA 目录中未找到 report.md。" />
          )}

          {data.figures.length > 0 && (
            <section className={styles.figures}>
              <h4 className="card__title">图表 ({data.figures.length})</h4>
              {data.figures.map((figure) => (
                <figure key={figure.name} className={styles.figure}>
                  <img
                    src={`data:${figure.mime};base64,${figure.data}`}
                    alt={figure.name}
                    className={styles.figureImg}
                  />
                  <figcaption className={styles.figureCaption}>{figure.name}</figcaption>
                </figure>
              ))}
            </section>
          )}
        </div>
      )}
    </div>
  );
}
