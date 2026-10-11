import { useEffect, useId, useMemo, useState } from "react";
import { errorMessage } from "../lib/errors";
import { experimentsList, graphAlgorithm, graphAlgorithms, type GraphAlgorithmInfo } from "../lib/tauri-bridge";
import { useRemoteData } from "../hooks/useRemoteData";
import { EmptyState } from "./common/EmptyState";
import { Icon } from "./common/Icon";
import styles from "./AlgorithmsPanel.module.css";

const DESCRIPTIONS: Record<string, string> = {
  topological_order: "按实验谱系的层级顺序排列全部假设",
  cycle_detect: "检查谱系与取代关系组成的图中是否存在环（防御性校验）",
  lineage: "某个实验到根实验的祖先链及对应假设",
  active_hypotheses: "选定父实验与子假设时仍然有效的假设集合",
  descendants: "某个实验派生出的全部后代实验",
  best_path: "当前 SOTA 实验及其祖先假设链",
  rank_pending: "按优先级排列尚待验证的假设",
  supersedes_closure: "某个假设直接或间接取代的全部假设",
  refutation_reachability: "某个假设被证伪时，沿谱系受影响的下游假设",
};

const PARAM_LABELS: Record<string, string> = {
  experiment_id: "实验 ID",
  hypothesis_id: "假设 ID",
  child_hypothesis_id: "子假设 ID",
};

/** Graph algorithm runner: pick an algorithm, fill parameters (with ID suggestions), see the result. */
export default function AlgorithmsPanel() {
  const algorithms = useRemoteData(() => graphAlgorithms().then((res) => res.algorithms), { follow: false });
  const experiments = useRemoteData(() => experimentsList().then((res) => res.experiments));
  const list = useMemo(() => algorithms.data ?? [], [algorithms.data]);
  const [selectedName, setSelectedName] = useState("");
  const [paramValues, setParamValues] = useState<Record<string, string>>({});
  const [result, setResult] = useState<unknown>(null);
  const [error, setError] = useState<string | null>(null);
  const [running, setRunning] = useState(false);
  const listId = useId();

  useEffect(() => {
    if (!selectedName && list.length > 0) setSelectedName(list[0].name);
  }, [list, selectedName]);

  const selected: GraphAlgorithmInfo | null = list.find((algo) => algo.name === selectedName) ?? null;
  const experimentIds = (experiments.data ?? []).map((item) => item.experiment.id);
  const hypothesisIds = (experiments.data ?? []).map((item) => item.hypothesis.id);
  const missingRequired = selected?.params.some((p) => p.required && !(paramValues[p.name] ?? "").trim()) ?? true;

  function choose(name: string) {
    setSelectedName(name);
    setParamValues({});
    setResult(null);
    setError(null);
  }

  async function run() {
    if (!selected) return;
    setRunning(true);
    setError(null);
    try {
      const params: Record<string, unknown> = {};
      for (const p of selected.params) params[p.name] = (paramValues[p.name] ?? "").trim();
      setResult(await graphAlgorithm(selected.name, params));
    } catch (err) {
      setError(errorMessage(err));
      setResult(null);
    } finally {
      setRunning(false);
    }
  }

  if (algorithms.loading && list.length === 0) return <EmptyState icon="loader" message="加载算法清单…" />;
  if (list.length === 0) {
    return <EmptyState icon="algorithms" title="暂无可用算法" message={algorithms.error ?? undefined} />;
  }

  return (
    <div className={styles.layout}>
      <ul className={styles.list} role="listbox" aria-label="图算法">
        {list.map((algo) => (
          <li key={algo.name}>
            <button
              type="button"
              role="option"
              aria-selected={algo.name === selectedName}
              className={styles.item}
              onClick={() => choose(algo.name)}
            >
              <span className={styles.itemTitle}>{algo.label}</span>
              <span className={styles.itemDesc}>{DESCRIPTIONS[algo.name] ?? algo.name}</span>
            </button>
          </li>
        ))}
      </ul>

      {selected && (
        <section className={styles.runner}>
          <header>
            <h3 className={styles.title}>{selected.label}</h3>
            <p className={styles.desc}>{DESCRIPTIONS[selected.name] ?? ""}</p>
          </header>

          {selected.params.length > 0 && (
            <div className={styles.params}>
              {selected.params.map((param) => {
                const suggestions = param.name.includes("experiment")
                  ? experimentIds
                  : param.name.includes("hypothesis")
                    ? hypothesisIds
                    : [];
                const datalist = suggestions.length > 0 ? `${listId}-${param.name}` : undefined;
                return (
                  <label key={param.name} className="field">
                    <span className="field__label">
                      {PARAM_LABELS[param.name] ?? param.name}
                      {param.required && <span className={styles.required}> *</span>}
                    </span>
                    <input
                      className="input input--mono"
                      type="text"
                      list={datalist}
                      value={paramValues[param.name] ?? ""}
                      onChange={(e) => setParamValues((prev) => ({ ...prev, [param.name]: e.target.value }))}
                      placeholder={param.required ? "必填" : "可选"}
                    />
                    {datalist && (
                      <datalist id={datalist}>
                        {Array.from(new Set(suggestions)).map((id) => (
                          <option key={id} value={id} />
                        ))}
                      </datalist>
                    )}
                  </label>
                );
              })}
            </div>
          )}

          <div>
            <button type="button" className="btn btn--primary btn--sm" onClick={() => void run()} disabled={running || missingRequired}>
              {running ? <Icon name="loader" size={13} className="icon--spin" /> : <Icon name="play" size={13} />}
              运行
            </button>
          </div>

          {error && (
            <div className="callout callout--error" role="alert">
              <Icon name="alert" size={15} />
              <div className="callout__body">{error}</div>
            </div>
          )}

          {result != null && (
            <div className={styles.result}>
              <span className="section-label">结果</span>
              <pre className="code-block">{JSON.stringify(result, null, 2)}</pre>
            </div>
          )}
        </section>
      )}
    </div>
  );
}
