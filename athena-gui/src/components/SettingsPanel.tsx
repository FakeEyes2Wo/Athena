import { useCallback, useEffect, useState } from "react";
import { errorMessage } from "../lib/errors";
import {
  DEFAULT_GUI_SETTINGS,
  setProjectRoot,
  settingsGet,
  settingsSet,
  type GuiSettings,
} from "../lib/tauri-bridge";

type WritableField =
  | "concurrency"
  | "search_limit"
  | "direction"
  | "tolerance"
  | "auto_validate"
  | "manual_mode";

/** Editable runtime settings form with read-only fields grayed out. */
export default function SettingsPanel() {
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
    <div className="detail-panel">
      <div className="panel-toolbar">
        <h3>运行设置</h3>
        <div className="panel-toolbar__actions">
          <button className="btn btn--subtle btn--sm" onClick={() => void refresh()}>刷新</button>
          <button className="btn btn--primary btn--sm" onClick={() => void save()} disabled={saving}>
            {saving ? "保存中…" : "保存"}
          </button>
        </div>
      </div>

      {error && <div className="card card--error" style={{ marginBottom: 12 }}>{error}</div>}
      {saved && !error && (
        <div style={{ marginBottom: 12 }}>
          <span className="badge badge--success">✓ 设置已保存</span>
        </div>
      )}

      <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr) minmax(0, 1fr)", gap: "16px 20px" }}>
        <div className="field" style={{ gridColumn: "1 / -1" }}>
          <span className="field__label">项目目录</span>
          <div style={{ display: "flex", gap: 8 }}>
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
        </div>

        <ReadOnlyField label="模型" value={settings.model ?? "—"} />

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

        <ReadOnlyField label="阶段" value={settings.phase} />
        <ReadOnlyField label="状态" value={settings.status} />
      </div>

      <p style={{ marginTop: 16, fontSize: 12, color: "var(--text-tertiary)" }}>
        提示：direction / tolerance / auto_validate 为构造期参数，改动后延迟生效（仅影响后续 plan）。
      </p>
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
