import { useCallback, useEffect, useState } from "react";
import { Button, Flash, FormControl, Select, TextInput } from "@primer/react";
import { errorMessage } from "../lib/errors";
import {
  graphAlgorithm,
  graphAlgorithms,
  type GraphAlgorithmInfo,
} from "../lib/tauri-bridge";
import { CodeBlock } from "./common/CodeBlock";
import { EmptyState } from "./common/EmptyState";
import { Panel } from "./common/Panel";
import styles from "./AlgorithmsPanel.module.css";

/** Dropdown + parameter form + JSON result runner for graph algorithms. */
export default function AlgorithmsPanel() {
  const [algorithms, setAlgorithms] = useState<GraphAlgorithmInfo[]>([]);
  const [selectedName, setSelectedName] = useState<string>("");
  const [paramValues, setParamValues] = useState<Record<string, string>>({});
  const [result, setResult] = useState<unknown>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [running, setRunning] = useState(false);

  const selected = algorithms.find((algo) => algo.name === selectedName) ?? null;

  const loadAlgorithms = useCallback(async () => {
    setLoading(true);
    try {
      const res = await graphAlgorithms();
      setAlgorithms(res.algorithms);
      if (res.algorithms.length > 0 && !selectedName) {
        setSelectedName(res.algorithms[0].name);
      }
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setLoading(false);
    }
  }, [selectedName]);

  useEffect(() => {
    void loadAlgorithms();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function handleSelect(name: string) {
    setSelectedName(name);
    setParamValues({});
    setResult(null);
    setError(null);
  }

  function handleParamChange(name: string, value: string) {
    setParamValues((prev) => ({ ...prev, [name]: value }));
  }

  async function run() {
    if (!selected) return;
    setRunning(true);
    setError(null);
    try {
      const params: Record<string, unknown> = {};
      for (const p of selected.params) {
        params[p.name] = paramValues[p.name] ?? "";
      }
      const res = await graphAlgorithm(selected.name, params);
      setResult(res);
    } catch (err) {
      setError(errorMessage(err));
      setResult(null);
    } finally {
      setRunning(false);
    }
  }

  return (
    <Panel title="图算法">
      <p className={styles.intro}>选择算法、填写参数后运行，结果以 JSON 展示。</p>

      {error && <Flash variant="danger">{error}</Flash>}

      {loading ? (
        <EmptyState icon="loader" message="加载算法清单…" />
      ) : algorithms.length === 0 ? (
        <EmptyState icon="algorithms" message="暂无可用算法。" />
      ) : (
        <>
          <div className={styles.runRow}>
            <FormControl>
              <FormControl.Label>算法</FormControl.Label>
              <Select value={selectedName} onChange={(e) => handleSelect(e.target.value)}>
                {algorithms.map((algo) => (
                  <Select.Option key={algo.name} value={algo.name}>
                    {algo.label}
                  </Select.Option>
                ))}
              </Select>
            </FormControl>
            <Button variant="primary" onClick={() => void run()} disabled={running || !selected}>
              {running ? "运行中…" : "运行"}
            </Button>
          </div>

          {selected && selected.params.length > 0 && (
            <div className={styles.params}>
              {selected.params.map((p) => (
                <FormControl key={p.name} required={p.required}>
                  <FormControl.Label>{p.name}</FormControl.Label>
                  <TextInput
                    block
                    value={paramValues[p.name] ?? ""}
                    onChange={(e) => handleParamChange(p.name, e.target.value)}
                    placeholder={p.required ? "必填" : "可选"}
                  />
                </FormControl>
              ))}
            </div>
          )}
        </>
      )}

      {result != null && <CodeBlock>{JSON.stringify(result, null, 2)}</CodeBlock>}
    </Panel>
  );
}
