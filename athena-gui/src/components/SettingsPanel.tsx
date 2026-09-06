import { useCallback, useEffect, useState } from "react";
import { errorMessage } from "../lib/errors";
import {
  DEFAULT_GUI_SETTINGS,
  setProjectRoot,
  settingsGet,
  settingsSet,
  type ComputeSettings,
  type GuiSettings,
} from "../lib/tauri-bridge";
import { Icon } from "./common/Icon";
import styles from "./SettingsPanel.module.css";

type WritableField =
  | "concurrency"
  | "search_limit"
  | "ideation"
  | "direction"
  | "tolerance"
  | "auto_validate"
  | "skip_validate"
  | "manual_mode"
  | "experiment_timeout_s";

interface SettingsPanelProps {
  onClose(): void;
}

function normalizeSettings(raw: Partial<GuiSettings> | GuiSettings): GuiSettings {
  return {
    ...DEFAULT_GUI_SETTINGS,
    ...raw,
    model_connection: {
      ...DEFAULT_GUI_SETTINGS.model_connection,
      ...(raw.model_connection ?? {}),
    },
    compute: {
      ...DEFAULT_GUI_SETTINGS.compute,
      ...(raw.compute ?? {}),
      hosts: raw.compute?.hosts ?? [],
    },
  };
}

function editableSettings(settings: GuiSettings): GuiSettings {
  return {
    ...settings,
    model_connection: { ...settings.model_connection, llm_api_key: "" },
  };
}

/** 运行设置：圆角纯白小窗，覆盖在主工作区之上。 */
export function SettingsPanel({ onClose }: SettingsPanelProps) {
  const [settings, setSettings] = useState<GuiSettings>(DEFAULT_GUI_SETTINGS);
  const [form, setForm] = useState<GuiSettings>(DEFAULT_GUI_SETTINGS);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [projectRoot, setProjectRootInput] = useState("");
  const [switching, setSwitching] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const next = normalizeSettings(await settingsGet());
      setSettings(next);
      setForm(editableSettings(next));
      setProjectRootInput(next.project_root || "");
      setError(null);
    } catch (err) {
      setError(errorMessage(err));
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  function patchField(field: WritableField, value: unknown) {
    setSaved(false);
    setForm((prev) => ({ ...prev, [field]: value }));
  }

  function patchModelConnection(
    field: keyof GuiSettings["model_connection"],
    value: string,
  ) {
    setSaved(false);
    setForm((prev) => ({
      ...prev,
      model_connection: { ...prev.model_connection, [field]: value },
    }));
  }

  function patchCompute(field: keyof ComputeSettings, value: unknown) {
    setSaved(false);
    setForm((prev) => ({
      ...prev,
      compute: { ...prev.compute, [field]: value },
    }));
  }

  async function save() {
    setSaving(true);
    setError(null);
    setSaved(false);
    const { llm_api_key, ...modelConnection } = form.model_connection;
    const modelConnectionPatch = llm_api_key.trim()
      ? { ...modelConnection, llm_api_key: llm_api_key.trim() }
      : modelConnection;
    const patch: Partial<GuiSettings> = {
      concurrency: form.concurrency,
      search_limit: form.search_limit,
      ideation: form.ideation,
      direction: form.direction,
      tolerance: form.tolerance,
      auto_validate: form.auto_validate,
      skip_validate: form.skip_validate,
      manual_mode: form.manual_mode,
      experiment_timeout_s: form.experiment_timeout_s,
      data_root: form.data_root,
      compute: form.compute,
      model_connection: modelConnectionPatch as GuiSettings["model_connection"],
    };
    try {
      const next = normalizeSettings(await settingsSet(patch));
      setSettings(next);
      setForm(editableSettings(next));
      setSaved(true);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setSaving(false);
    }
  }

  async function switchProject() {
    setSwitching(true);
    setError(null);
    setSaved(false);
    try {
      const next = normalizeSettings(await setProjectRoot(projectRoot));
      setSettings(next);
      setForm(editableSettings(next));
      setProjectRootInput(next.project_root || "");
      setSaved(true);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setSwitching(false);
    }
  }

  return (
    <div
      className={styles.overlay}
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <section className={styles.dialog} role="dialog" aria-modal="true" aria-label="运行设置">
        <header className={styles.header}>
          <div>
            <h2>运行设置</h2>
            <p className={styles.subtitle}>配置 Athena 运行参数与项目目录</p>
          </div>
          <button type="button" className={styles.close} onClick={onClose} aria-label="关闭设置">
            <Icon name="close" size={16} />
          </button>
        </header>

        {error && <div className="card card--error" style={{ marginBottom: 12 }}>{error}</div>}
        {saved && !error && (
          <div style={{ marginBottom: 12 }}>
            <span className="badge badge--success">✓ 设置已保存</span>
          </div>
        )}

        <div className={styles.body}>
          <section className={styles.section}>
            <h3 className={styles.sectionTitle}>项目</h3>
            <div className={styles.projectRow}>
              <input
                className="input"
                type="text"
                placeholder="选择项目目录…"
                value={projectRoot}
                onChange={(e) => setProjectRootInput(e.target.value)}
              />
              <button
                className="btn btn--primary btn--sm"
                onClick={() => void switchProject()}
                disabled={switching || !projectRoot.trim()}
              >
                {switching ? "切换中…" : "切换"}
              </button>
            </div>
          </section>

          <section className={styles.section}>
            <h3 className={styles.sectionTitle}>模型连接</h3>
            <div className={styles.grid}>
              <label className="field">
                <span className="field__label">PROVIDER</span>
                <select
                  className="select"
                  value={form.model_connection.provider}
                  onChange={(e) => patchModelConnection("provider", e.target.value)}
                >
                  <option value="deepseek">deepseek</option>
                  <option value="openai">openai</option>
                  <option value="qwen">qwen</option>
                </select>
              </label>

              <label className="field">
                <span className="field__label">MODEL_NAME</span>
                <input
                  className="input"
                  type="text"
                  placeholder="deepseek-v4-flash"
                  value={form.model_connection.model_name}
                  onChange={(e) => patchModelConnection("model_name", e.target.value)}
                />
              </label>

              <label className="field" style={{ gridColumn: "1 / -1" }}>
                <span className="field__label">BASE_URL</span>
                <input
                  className="input"
                  type="text"
                  placeholder={
                    form.model_connection.provider === "deepseek"
                      ? "https://api.deepseek.com"
                      : form.model_connection.provider === "openai"
                        ? "https://api.openai.com/v1"
                        : "https://dashscope.aliyuncs.com/compatible-mode/v1"
                  }
                  value={form.model_connection.base_url}
                  onChange={(e) => patchModelConnection("base_url", e.target.value)}
                />
              </label>

              <label className="field" style={{ gridColumn: "1 / -1" }}>
                <span className="field__label">LLM_API_KEY</span>
                <input
                  className="input"
                  type="password"
                  autoComplete="off"
                  placeholder={
                    settings.model_connection.llm_api_key
                      ? `已设置 · ${settings.model_connection.llm_api_key}`
                      : "未设置"
                  }
                  value={form.model_connection.llm_api_key}
                  onChange={(e) => patchModelConnection("llm_api_key", e.target.value)}
                />
              </label>
            </div>
            <p className={styles.hint}>密钥只写回 .env，界面不回显明文；留空表示不修改。</p>
          </section>

          <section className={styles.section}>
            <h3 className={styles.sectionTitle}>运行参数</h3>
            <div className={styles.grid}>
              <label className="field">
                <span className="field__label">并发度</span>
                <input
                  className="input"
                  type="number"
                  min={1}
                  value={form.concurrency}
                  onChange={(e) => patchField("concurrency", Number(e.target.value))}
                />
              </label>

              <label className="field">
                <span className="field__label">搜索上限</span>
                <input
                  className="input"
                  type="number"
                  min={0}
                  value={form.search_limit}
                  onChange={(e) => patchField("search_limit", Number(e.target.value))}
                />
              </label>

              <label className="field">
                <span className="field__label">Ideator 机制</span>
                <select
                  className="select"
                  value={form.ideation}
                  onChange={(e) => patchField("ideation", e.target.value)}
                >
                  <option value="ideageneration">ideageneration · 门禁生成</option>
                  <option value="baseline">baseline · 消融对照</option>
                  <option value="debate">debate · 辩论式</option>
                </select>
              </label>

              <label className="field">
                <span className="field__label">优化方向</span>
                <select
                  className="select"
                  value={form.direction}
                  onChange={(e) => patchField("direction", e.target.value)}
                >
                  <option value="maximize">maximize</option>
                  <option value="minimize">minimize</option>
                </select>
              </label>

              <label className="field">
                <span className="field__label">容忍度</span>
                <input
                  className="input"
                  type="number"
                  min={0}
                  step="0.01"
                  value={form.tolerance}
                  onChange={(e) => patchField("tolerance", Number(e.target.value))}
                />
              </label>

              <label className="field">
                <span className="field__label">实验超时（秒）</span>
                <input
                  className="input"
                  type="number"
                  min={1}
                  value={form.experiment_timeout_s}
                  onChange={(e) =>
                    patchField("experiment_timeout_s", Number(e.target.value))
                  }
                />
              </label>
            </div>
          </section>

          <section className={styles.section}>
            <h3 className={styles.sectionTitle}>算力 / Compute</h3>
            <div className={styles.grid}>
              <label className="field">
                <span className="field__label">模式</span>
                <select
                  className="select"
                  value={form.compute.mode}
                  onChange={(e) => patchCompute("mode", e.target.value)}
                >
                  <option value="local">local · 本机</option>
                  <option value="ssh">ssh · 远程 GPU</option>
                </select>
              </label>

              <label className="field">
                <span className="field__label">放置策略</span>
                <select
                  className="select"
                  value={form.compute.placement}
                  onChange={(e) => patchCompute("placement", e.target.value)}
                >
                  <option value="pack">pack</option>
                  <option value="spread">spread</option>
                  <option value="homogeneous">homogeneous</option>
                </select>
              </label>

              <label className="field">
                <span className="field__label">拿不到卡时</span>
                <select
                  className="select"
                  value={form.compute.fallback}
                  onChange={(e) => patchCompute("fallback", e.target.value)}
                >
                  <option value="never">never · 排队/失败</option>
                  <option value="ask">ask · 询问</option>
                </select>
              </label>

              <label className="field">
                <span className="field__label">每实验 GPU 数</span>
                <input
                  className="input"
                  type="number"
                  min={1}
                  value={form.compute.gpus_per_experiment}
                  onChange={(e) =>
                    patchCompute("gpus_per_experiment", Number(e.target.value))
                  }
                />
              </label>

              <label className="field">
                <span className="field__label">排队超时（秒）</span>
                <input
                  className="input"
                  type="number"
                  min={0}
                  value={form.compute.queue_timeout_s ?? ""}
                  onChange={(e) => {
                    const value = e.target.value;
                    patchCompute(
                      "queue_timeout_s",
                      value === "" ? null : Number(value),
                    );
                  }}
                />
              </label>

              <label className="field" style={{ gridColumn: "1 / -1" }}>
                <span className="field__label">数据集根目录</span>
                <input
                  className="input"
                  type="text"
                  placeholder="留空表示不设置"
                  value={form.data_root ?? ""}
                  onChange={(e) =>
                    setForm((prev) => ({
                      ...prev,
                      data_root: e.target.value.trim() || null,
                    }))
                  }
                />
              </label>
            </div>
            {form.compute.hosts.length > 0 && (
              <div className={styles.grid}>
                {form.compute.hosts.map((host) => (
                  <div key={host.name} className="field">
                    <span className="field__label">{host.name}</span>
                    <div className="input input--readonly">
                      {host.ssh} · {host.gpus === "auto" ? "auto" : host.gpus.join(",")} · {host.max_leases} leases
                    </div>
                  </div>
                ))}
              </div>
            )}
          </section>

          <section className={styles.section}>
            <h3 className={styles.sectionTitle}>开关</h3>
            <div className={styles.switches}>
              <label
                className={form.skip_validate ? `switch ${styles.switchDisabled}` : "switch"}
              >
                <input
                  type="checkbox"
                  checked={form.auto_validate}
                  disabled={form.skip_validate}
                  onChange={(e) => patchField("auto_validate", e.target.checked)}
                />
                <span className="switch__track" />
                自动验证
              </label>

              <label className="switch">
                <input
                  type="checkbox"
                  checked={form.skip_validate}
                  onChange={(e) => patchField("skip_validate", e.target.checked)}
                  aria-describedby="skip-validate-help"
                />
                <span className="switch__track" />
                跳过 VALIDATE
              </label>

              <p
                id="skip-validate-help"
                className={`${styles.hint} ${styles.skipValidateHelp}`}
              >
                SEARCH 完成后将直接生成 Final，不会运行独立最终评估，也不会产生最终测试或泛化指标。
              </p>

              <label className="switch">
                <input
                  type="checkbox"
                  checked={form.manual_mode}
                  onChange={(e) => patchField("manual_mode", e.target.checked)}
                />
                <span className="switch__track" />
                手动选假设
              </label>
            </div>
          </section>

          <section className={styles.section}>
            <h3 className={styles.sectionTitle}>只读状态</h3>
            <div className={styles.grid}>
              <ReadOnlyField label="模型" value={settings.model ?? "—"} />
              <ReadOnlyField label="阶段" value={settings.phase} />
              <ReadOnlyField label="状态" value={settings.status} />
            </div>
          </section>
        </div>

        <footer className={styles.footer}>
          <div className={styles.actions} style={{ marginLeft: "auto" }}>
            <button className="btn btn--ghost btn--sm" onClick={() => void refresh()}>刷新</button>
            <button className="btn btn--primary btn--sm" onClick={() => void save()} disabled={saving}>
              {saving ? "保存中…" : "保存"}
            </button>
          </div>
        </footer>
      </section>
    </div>
  );
}

function ReadOnlyField({ label, value }: { label: string; value: string }) {
  return (
    <div className="field">
      <span className="field__label">{label}</span>
      <div className="input input--readonly">{value}</div>
    </div>
  );
}

export default SettingsPanel;
