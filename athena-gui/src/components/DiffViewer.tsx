import { useCallback, useEffect, useState } from "react";
import { Button, Flash } from "@primer/react";
import { errorMessage } from "../lib/errors";
import { experimentsList, type ExperimentDetail } from "../lib/tauri-bridge";
import { EmptyState } from "./common/EmptyState";
import { KeyValueList } from "./common/KeyValueList";
import { Panel } from "./common/Panel";
import { Section } from "./common/Section";
import styles from "./DiffViewer.module.css";

/** Best experiment's change spec: what changed, acceptance rule, and rubrics. */
export default function DiffViewer() {
  const [detail, setDetail] = useState<ExperimentDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const res = await experimentsList();
      const sota = res.experiments.find((d) => d.sota);
      const target = sota ?? res.experiments[res.experiments.length - 1] ?? null;
      setDetail(target);
      setError(null);
    } catch (err) {
      setError(errorMessage(err));
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const plan = detail?.experiment.plan;

  return (
    <Panel
      title="变更详情"
      actions={
        <Button size="small" onClick={() => void refresh()}>
          刷新
        </Button>
      }
    >
      {error && <Flash variant="danger">{error}</Flash>}

      {!detail || !plan ? (
        <EmptyState icon="diff" message="暂无实验。运行搜索后此处展示最佳实验的变更规格。" />
      ) : (
        <>
          <Section title="实验">
            <p className={styles.mono}>
              {detail.experiment.id}
              {detail.sota ? " ★ SOTA" : ""}
            </p>
            <h4 className={styles.subheading}>变更内容</h4>
            <p className={styles.text}>{plan.change || "—"}</p>
            <h4 className={styles.subheading}>验收标准</h4>
            <p className={styles.text}>{plan.acceptance_rule || "—"}</p>
            {plan.rubrics.length > 0 && (
              <>
                <h4 className={styles.subheading}>评价指标</h4>
                <ul className={styles.rubrics}>
                  {plan.rubrics.map((rubric) => (
                    <li key={rubric}>{rubric}</li>
                  ))}
                </ul>
              </>
            )}
          </Section>

          <Section title="工作区">
            <KeyValueList
              items={[
                { label: "路径", value: detail.experiment.gitwork.path, mono: true },
                { label: "分支", value: detail.experiment.gitwork.branch, mono: true },
                { label: "基线提交", value: detail.experiment.gitwork.base_commit, mono: true },
              ]}
            />
          </Section>
        </>
      )}
    </Panel>
  );
}
