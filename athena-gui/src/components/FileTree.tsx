import { useCallback, useEffect, useState } from "react";
import { Button, Flash } from "@primer/react";
import { errorMessage } from "../lib/errors";
import { experimentsList, type ExperimentDetail } from "../lib/tauri-bridge";
import { EmptyState } from "./common/EmptyState";
import { KeyValueList } from "./common/KeyValueList";
import { Panel } from "./common/Panel";
import { Section } from "./common/Section";

/** Best experiment's workspace and produced artifacts. */
export default function FileTree() {
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

  const artifacts = detail ? Object.entries(detail.experiment.artifacts) : [];

  return (
    <Panel
      title="工作区与产物"
      actions={
        <Button size="small" onClick={() => void refresh()}>
          刷新
        </Button>
      }
    >
      {error && <Flash variant="danger">{error}</Flash>}

      {!detail ? (
        <EmptyState icon="files" message="暂无实验。运行搜索后此处展示最佳实验的工作区与产物。" />
      ) : (
        <>
          <Section title="工作区">
            <KeyValueList
              items={[
                { label: "路径", value: detail.experiment.gitwork.path, mono: true },
                { label: "分支", value: detail.experiment.gitwork.branch, mono: true },
                { label: "基线提交", value: detail.experiment.gitwork.base_commit, mono: true },
              ]}
            />
          </Section>

          <Section title={`产物 (${artifacts.length})`}>
            {artifacts.length === 0 ? (
              <EmptyState icon="files" message="暂无产物引用。" />
            ) : (
              <KeyValueList items={artifacts.map(([kind, ref]) => ({ label: kind, value: ref, mono: true }))} />
            )}
          </Section>
        </>
      )}
    </Panel>
  );
}
