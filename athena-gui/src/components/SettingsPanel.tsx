import { useCallback, useEffect, useState } from "react";
import { errorMessage } from "../lib/errors";
import {
  DEFAULT_GUI_SETTINGS,
  setProjectRoot,
  settingsGet,
  settingsSet,
  type GuiSettings,
} from "../lib/tauri-bridge";
import { Icon } from "./common/Icon";
import styles from "./SettingsPanel.module.css";

type WritableField =
  | "concurrency"
  | "search_limit"
  | "direction"
  | "tolerance"
  | "auto_validate"
  | "manual_mode";

interface SettingsPanelProps {
  onClose(): void;
}

/** 运行设置：圆角米白小窗，覆盖在主工作区之上。 */
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
      const next = await settingsGet();
      setSettings(next);
      setForm(next);
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

  function patchApiKey(field: keyof GuiSettings["api_keys"], value: string) {
    setSaved(false);
    setForm((prev) => ({
      ...prev,
      api_keys: { ...prev.api_keys, [field]: value },
    }));
  }

  async function save() {
    setSaving(true);
    setError(null);
    setSaved(false);
    const patch: Partial<GuiSettings> = {
      concurrency: form.concurrency,
      search_limit: form.search_limit,
      direction: form.direction,
      tolerance: form.tolerance,
      auto_validate: form.auto_validate,
      manual_mode: form.manual_mode,
      api_keys: form.api_keys,
    };
    try {
      const next = await settingsSet(patch);
      setSettings(next);
      setForm(next);
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
      const next = await setProjectRoot(projectRoot);
      setSettings(next);
      setForm(next);
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
            <h3 className={styles.sectionTitle}>API Keys</h3>
            <div className={styles.grid}>
              <ApiKeyField
                label="DeepSeek"
                field="DEEPSEEK_API_KEY"
                masked={settings.api_keys.DEEPSEEK_API_KEY}
                value={form.api_keys.DEEPSEEK_API_KEY}
                onChange={patchApiKey}
              />
              <ApiKeyField
                label="OpenAI"
                field="OPENAI_API_KEY"
                masked={settings.api_keys.OPENAI_API_KEY}
                value={form.api_keys.OPENAI_API_KEY}
                onChange={patchApiKey}
              />
              <ApiKeyField
                label="Kaggle Token"
                field="KAGGLE_API_TOKEN"
                masked={settings.api_keys.KAGGLE_API_TOKEN}
                value={form.api_keys.KAGGLE_API_TOKEN}
                onChange={patchApiKey}
              />
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
            </div>
          </section>

          <section className={styles.section}>
            <h3 className={styles.sectionTitle}>开关</h3>
            <div className={styles.switches}>
              <label className="switch">
                <input
                  type="checkbox"
                  checked={form.auto_validate}
                  onChange={(e) => patchField("auto_validate", e.target.checked)}
                />
                <span className="switch__track" />
                自动验证
              </label>

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
          <p className={styles.hint}>
            提示：direction / tolerance / auto_validate 为构造期参数，保存后对后续 plan 生效。
          </p>
          <div className={styles.actions}>
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

function ApiKeyField({
  label,
  field,
  masked,
  value,
  onChange,
}: {
  label: string;
  field: keyof GuiSettings["api_keys"];
  masked: string;
  value: string;
  onChange(field: keyof GuiSettings["api_keys"], value: string): void;
}) {
  return (
    <label className="field">
      <span className="field__label">{label}</span>
      <input
        className="input"
        type="password"
        autoComplete="off"
        placeholder={masked ? `已设置 · ${masked}` : "未设置"}
        value={value}
        onChange={(e) => onChange(field, e.target.value)}
      />
    </label>
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
