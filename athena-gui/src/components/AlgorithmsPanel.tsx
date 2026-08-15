import { useCallback, useEffect, useState } from "react";
import { errorMessage } from "../lib/errors";
import {
  graphAlgorithm,
  graphAlgorithms,
  type GraphAlgorithmInfo,
} from "../lib/tauri-bridge";
import { EmptyState } from "./common/EmptyState";

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
    <div className="detail-panel">
      <div className="panel-toolbar">
        <h3>图算法</h3>
      </div>
      <p style={{ marginBottom: 12 }}>选择算法、填写参数后运行，结果以 JSON 展示。</p>

      {error && <div className="card card--error" style={{ marginBottom: 12 }}>{error}</div>}

      {loading ? (
        <EmptyState icon="loader" message="加载算法清单…" />
      ) : algorithms.length === 0 ? (
        <EmptyState icon="algorithms" message="暂无可用算法。" />
      ) : (
        <>
          <div style={{ display: "flex", gap: 8, marginBottom: 12, alignItems: "center", flexWrap: "wrap" }}>
            <select
              className="select"
              style={{ width: "auto", minWidth: 220 }}
              value={selectedName}
              onChange={(e) => handleSelect(e.target.value)}
            >
              {algorithms.map((algo) => (
                <option key={algo.name} value={algo.name}>
                  {algo.label}
                </option>
              ))}
            </select>
            <button className="btn btn--primary btn--sm" onClick={() => void run()} disabled={running || !selected}>
              {running ? "运行中…" : "运行"}
            </button>
          </div>

          {selected && selected.params.length > 0 && (
            <div style={{ display: "flex", flexDirection: "column", gap: 8, marginBottom: 12 }}>
              {selected.params.map((p) => (
                <label key={p.name} className="field" style={{ flexDirection: "row", alignItems: "center" }}>
                  <span className="field__label" style={{ minWidth: 180 }}>{p.name}</span>
                  <input
                    className="input"
                    type="text"
                    value={paramValues[p.name] ?? ""}
                    onChange={(e) => handleParamChange(p.name, e.target.value)}
                    placeholder={p.required ? "必填" : "可选"}
                  />
                </label>
              ))}
            </div>
          )}
        </>
      )}

      {result != null && <pre className="code-block">{JSON.stringify(result, null, 2)}</pre>}
    </div>
  );
}
