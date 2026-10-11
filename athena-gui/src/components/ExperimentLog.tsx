import { useCallback, useEffect, useState } from "react";
import { Button, Flash, Timeline } from "@primer/react";
import { errorMessage } from "../lib/errors";
import { experimentsList, type ExperimentDetail } from "../lib/tauri-bridge";
import { EmptyState } from "./common/EmptyState";
import { Icon } from "./common/Icon";
import { Panel } from "./common/Panel";
import { StatusBadge } from "./common/StatusBadge";
import styles from "./ExperimentLog.module.css";

function primaryOf(d: ExperimentDetail): string {
  return d.experiment.eval?.primary != null ? d.experiment.eval.primary.toFixed(4) : "—";
}

function winnerOf(d: ExperimentDetail): string {
  return d.experiment.verdict?.winner ?? "—";
}

/** Compact, ordered timeline of experiment lifecycle events. */
export default function ExperimentLog() {
  const [experiments, setExperiments] = useState<ExperimentDetail[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const res = await experimentsList();
      setExperiments(res.experiments);
      setError(null);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return (
    <Panel
      title={`实验日志 (${experiments.length})`}
      actions={
        <Button size="small" onClick={() => void refresh()} disabled={loading}>
          {loading ? "加载中…" : "刷新"}
        </Button>
      }
    >
      {error && <Flash variant="danger">{error}</Flash>}

      {experiments.length === 0 ? (
        <EmptyState icon="log" message="暂无实验记录。启动搜索后将在此处按序展示实验生命周期。" />
      ) : (
        <Timeline>
          {experiments.map((d) => (
            <Timeline.Item key={d.experiment.id} condensed>
              <Timeline.Badge>
                <Icon name="experiments" size={14} />
              </Timeline.Badge>
              <Timeline.Body>
                <div className={styles.head}>
                  <span className={styles.id}>
                    {d.experiment.id}
                    {d.sota ? " ★" : ""}
                  </span>
                  <StatusBadge status={d.experiment.status} />
                  <span>primary {primaryOf(d)}</span>
                  <span>winner {winnerOf(d)}</span>
                </div>
                <div className={styles.statement}>{d.hypothesis.statement}</div>
              </Timeline.Body>
            </Timeline.Item>
          ))}
        </Timeline>
      )}
    </Panel>
  );
}
