import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { errorMessage } from "../lib/errors";
import {
  DEFAULT_GUI_SETTINGS,
  settingsGet,
  settingsSet,
  type ComputeSettings,
  type GuiSettings,
} from "../lib/tauri-bridge";
import { phaseLabel } from "../lib/labels";
import { useTheme, type ThemePreference } from "../hooks/useTheme";
import { Dialog } from "./common/Dialog";
import { Icon, type IconName } from "./common/Icon";
import styles from "./SettingsPanel.module.css";

interface SettingsPanelProps {
  /** Defaults to open so the panel can be rendered standalone. */
  open?: boolean;
  onClose(): void;
  /** Called with the saved snapshot after a successful save. */
  onSaved?(settings: GuiSettings): void;
  /** Opens the workspace picker (project switching lives there). */
  onSwitchWorkspace?(): void;
}

/** Editable copy of the settings; ``data_root`` stays raw text while typing. */
interface SettingsForm extends Omit<GuiSettings, "data_root"> {
  data_root: string;
}

const SECTIONS: Array<{ id: string; label: string; icon: IconName }> = [
  { id: "model", label: "模型连接", icon: "key" },
  { id: "research", label: "研究策略", icon: "sliders" },
  { id: "compute", label: "数据与算力", icon: "cpu" },
  { id: "appearance", label: "外观", icon: "sun" },
  { id: "workspace", label: "工作区", icon: "folder" },
];

const BASE_URL_PLACEHOLDER: Record<string, string> = {
  deepseek: "https://api.deepseek.com",
  openai: "https://api.openai.com/v1",
  qwen: "https://dashscope.aliyuncs.com/compatible-mode/v1",
};


/** What each hypothesis-intake mode does (mirrors ``athena run --ideation`` help). */
const IDEATION_HINT: Record<GuiSettings["ideation"], string> = {
  ideageneration: "假设进入研究树前先经过结构与可证伪性检查及多视角评审。",
  debate: "提出、评审、修订、裁决多轮辩论后再采纳假设，更慢但更审慎。",
  baseline: "不做筛选，直接采纳 Ideator 的输出，用于消融实验对照。",
};
function normalizeSettings(raw: Partial<GuiSettings> | GuiSettings): GuiSettings {
  return {
    ...DEFAULT_GUI_SETTINGS,
    ...raw,
    model_connection: { ...DEFAULT_GUI_SETTINGS.model_connection, ...(raw.model_connection ?? {}) },
    compute: {
      ...DEFAULT_GUI_SETTINGS.compute,
      ...(raw.compute ?? {}),
      hosts: raw.compute?.hosts ?? [],
    },
  };
}

function toForm(settings: GuiSettings): SettingsForm {
  return {
    ...settings,
    data_root: settings.data_root ?? "",
    // The masked key is only shown as a placeholder; never echo it back as input.
    model_connection: { ...settings.model_connection, llm_api_key: "" },
  };
}

function isInt(value: number, min: number): boolean {
  return Number.isInteger(value) && value >= min;
}

function validate(form: SettingsForm): Record<string, string> {
  const errors: Record<string, string> = {};
  if (!isInt(form.concurrency, 1)) errors.concurrency = "请输入不小于 1 的整数";
  if (!isInt(form.search_limit, 0)) errors.search_limit = "请输入不小于 0 的整数";
  if (!(Number.isFinite(form.tolerance) && form.tolerance >= 0)) errors.tolerance = "请输入不小于 0 的数值";
  if (!isInt(form.experiment_timeout_s, 1)) errors.experiment_timeout_s = "请输入不小于 1 的整数（秒）";
  if (!isInt(form.compute.gpus_per_experiment, 1)) errors.gpus_per_experiment = "请输入不小于 1 的整数";
  const queue = form.compute.queue_timeout_s;
  if (queue !== null && !(Number.isFinite(queue) && queue >= 0)) errors.queue_timeout_s = "留空或输入不小于 0 的数值";
  return errors;
}

/** Settings dialog: model connection, research policy, data & compute, appearance. */
export function SettingsPanel({ open = true, onClose, onSaved, onSwitchWorkspace }: SettingsPanelProps) {
  const [settings, setSettings] = useState<GuiSettings>(DEFAULT_GUI_SETTINGS);
  const [form, setForm] = useState<SettingsForm>(() => toForm(DEFAULT_GUI_SETTINGS));
  const [baseline, setBaseline] = useState<string>(() => JSON.stringify(toForm(DEFAULT_GUI_SETTINGS)));
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [section, setSection] = useState(SECTIONS[0].id);
  const bodyRef = useRef<HTMLDivElement | null>(null);
  const jumpingRef = useRef(false);
  const jumpTimerRef = useRef<number | undefined>(undefined);
  const { preference, setPreference } = useTheme();

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const next = normalizeSettings(await settingsGet());
      const nextForm = toForm(next);
      setSettings(next);
      setForm(nextForm);
      setBaseline(JSON.stringify(nextForm));
      setError(null);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!open) return;
    setSaved(false);
    void load();
  }, [load, open]);

  const errors = useMemo(() => validate(form), [form]);
  const dirty = JSON.stringify(form) !== baseline;
  const invalid = Object.keys(errors).length > 0;

  function patch<K extends keyof SettingsForm>(field: K, value: SettingsForm[K]) {
    setSaved(false);
    setForm((prev) => ({ ...prev, [field]: value }));
  }

  function patchModel(field: keyof GuiSettings["model_connection"], value: string) {
    setSaved(false);
    setForm((prev) => ({ ...prev, model_connection: { ...prev.model_connection, [field]: value } }));
  }

  function patchCompute<K extends keyof ComputeSettings>(field: K, value: ComputeSettings[K]) {
    setSaved(false);
    setForm((prev) => ({ ...prev, compute: { ...prev.compute, [field]: value } }));
  }

  async function save() {
    if (invalid) return;
    setSaving(true);
    setError(null);
    setSaved(false);
    const { llm_api_key, ...modelConnection } = form.model_connection;
    const key = llm_api_key.trim();
    const patchBody: Partial<GuiSettings> = {
      concurrency: form.concurrency,
      search_limit: form.search_limit,
      ideation: form.ideation,
      direction: form.direction,
      tolerance: form.tolerance,
      auto_validate: form.auto_validate,
      skip_validate: form.skip_validate,
      manual_mode: form.manual_mode,
      experiment_timeout_s: form.experiment_timeout_s,
      data_root: form.data_root.trim() || null,
      compute: form.compute,
      model_connection: (key ? { ...modelConnection, llm_api_key: key } : modelConnection) as GuiSettings["model_connection"],
    };
    try {
      const next = normalizeSettings(await settingsSet(patchBody));
      const nextForm = toForm(next);
      setSettings(next);
      setForm(nextForm);
      setBaseline(JSON.stringify(nextForm));
      setSaved(true);
      onSaved?.(next);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setSaving(false);
    }
  }

  function jumpTo(id: string) {
    setSection(id);
    // The smooth scroll would otherwise re-highlight whichever section passes by
    // (and the last sections can never reach the top of the body).
    jumpingRef.current = true;
    window.clearTimeout(jumpTimerRef.current);
    jumpTimerRef.current = window.setTimeout(() => {
      jumpingRef.current = false;
    }, 700);
    bodyRef.current?.querySelector(`[data-section="${id}"]`)?.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  // Highlight the section currently scrolled into view.
  function onScroll() {
    const body = bodyRef.current;
    if (!body || jumpingRef.current) return;
    if (body.scrollTop + body.clientHeight >= body.scrollHeight - 2) {
      setSection(SECTIONS[SECTIONS.length - 1].id);
      return;
    }
    const top = body.getBoundingClientRect().top;
    let current = SECTIONS[0].id;
    for (const item of SECTIONS) {
      const el = body.querySelector<HTMLElement>(`[data-section="${item.id}"]`);
      if (el && el.getBoundingClientRect().top - top < 80) current = item.id;
    }
    setSection(current);
  }

  const maskedKey = settings.model_connection.llm_api_key;

  return (
    <Dialog
      open={open}
      title="设置"
      onClose={onClose}
      size="lg"
      className={styles.dialog}
      bodyClassName={styles.dialogBody}
      footer={
        <>
          <span className={styles.status} role="status">
            {saved && !dirty ? (
              <span className={styles.saved}>
                <Icon name="success" size={14} /> 设置已保存
              </span>
            ) : dirty ? (
              "有未保存的修改"
            ) : null}
          </span>
          <button type="button" className="btn btn--ghost" onClick={() => void load()} disabled={loading || saving}>
            重新载入
          </button>
          <button type="button" className="btn" onClick={onClose}>
            {dirty ? "放弃修改" : "关闭"}
          </button>
          <button
            type="button"
            className="btn btn--primary"
            onClick={() => void save()}
            disabled={saving || loading || invalid}
          >
            {saving ? "保存中…" : "保存"}
          </button>
        </>
      }
    >
      <nav className={styles.nav} aria-label="设置分区">
        {SECTIONS.map((item) => (
          <button
            key={item.id}
            type="button"
            className={styles.navItem}
            aria-current={section === item.id ? "true" : undefined}
            onClick={() => jumpTo(item.id)}
          >
            <Icon name={item.icon} size={15} />
            {item.label}
          </button>
        ))}
      </nav>

      <div className={styles.body} ref={bodyRef} onScroll={onScroll}>
        {error && (
          <div className="callout callout--error" role="alert">
            <Icon name="alert" size={15} />
            <div className="callout__body">{error}</div>
          </div>
        )}

        <Section id="model" title="模型连接" description="Athena 通过这里配置的大模型服务完成任务理解、提出假设与编写实验代码。对所有会话生效。">
          <div className={styles.grid}>
            <Field label="服务商">
              <select
                className="select"
                value={form.model_connection.provider}
                onChange={(e) => patchModel("provider", e.target.value)}
              >
                <option value="deepseek">DeepSeek</option>
                <option value="openai">OpenAI</option>
                <option value="qwen">通义千问（DashScope）</option>
              </select>
            </Field>
            <Field label="模型名称">
              <input
                className="input"
                type="text"
                placeholder="例如 deepseek-chat"
                value={form.model_connection.model_name}
                onChange={(e) => patchModel("model_name", e.target.value)}
              />
            </Field>
            <Field label="Base URL" wide hint="留空则使用服务商默认地址。更换服务商时请同时修改模型名称与 Base URL。">
              <input
                className="input input--mono"
                type="text"
                placeholder={BASE_URL_PLACEHOLDER[form.model_connection.provider] ?? "https://"}
                value={form.model_connection.base_url}
                onChange={(e) => patchModel("base_url", e.target.value)}
              />
            </Field>
            <Field
              label="API Key"
              wide
              hint="密钥只写入本机 .env，界面不会回显明文。留空表示保持不变。"
            >
              <input
                className="input input--mono"
                type="password"
                autoComplete="off"
                aria-label="API Key"
                placeholder={maskedKey ? `已设置 · ${maskedKey}` : "尚未设置"}
                value={form.model_connection.llm_api_key}
                onChange={(e) => patchModel("llm_api_key", e.target.value)}
              />
            </Field>
          </div>
        </Section>

        <Section id="research" title="研究策略" description="控制搜索规模、假设生成方式与验证流程。仅作用于当前会话，新会话从默认值开始。">
          <div className={styles.grid}>
            <Field label="搜索上限" hint="SEARCH 阶段最多尝试的实验次数（0 表示不进行搜索）" error={errors.search_limit}>
              <NumberInput value={form.search_limit} min={0} onChange={(v) => patch("search_limit", v)} invalid={Boolean(errors.search_limit)} />
            </Field>
            <Field label="并发数" hint="同时运行的实验 worker 数量" error={errors.concurrency}>
              <NumberInput value={form.concurrency} min={1} onChange={(v) => patch("concurrency", v)} invalid={Boolean(errors.concurrency)} />
            </Field>
            <Field label="假设生成方式" hint={IDEATION_HINT[form.ideation]}>
              <select className="select" value={form.ideation} onChange={(e) => patch("ideation", e.target.value as GuiSettings["ideation"])}>
                <option value="ideageneration">质量把关（默认）</option>
                <option value="debate">辩论评审</option>
                <option value="baseline">直接采纳（消融对照）</option>
              </select>
            </Field>
            <Field label="优化方向">
              <select className="select" value={form.direction} onChange={(e) => patch("direction", e.target.value as GuiSettings["direction"])}>
                <option value="maximize">越大越好（maximize）</option>
                <option value="minimize">越小越好（minimize）</option>
              </select>
            </Field>
            <Field label="容忍度" hint="判定改进时允许的指标波动" error={errors.tolerance}>
              <NumberInput value={form.tolerance} min={0} step="0.01" onChange={(v) => patch("tolerance", v)} invalid={Boolean(errors.tolerance)} />
            </Field>
            <Field label="单次实验超时（秒）" error={errors.experiment_timeout_s}>
              <NumberInput
                value={form.experiment_timeout_s}
                min={1}
                onChange={(v) => patch("experiment_timeout_s", v)}
                invalid={Boolean(errors.experiment_timeout_s)}
              />
            </Field>
          </div>

          <div className={styles.toggles}>
            <Toggle
              label="自动验证"
              description="SEARCH 结束后自动进入 VALIDATE；关闭时需要人工确认"
              checked={form.auto_validate}
              disabled={form.skip_validate}
              onChange={(v) => patch("auto_validate", v)}
            />
            <Toggle
              label="跳过 VALIDATE"
              checked={form.skip_validate}
              onChange={(v) => patch("skip_validate", v)}
              describedBy="skip-validate-help"
              description={
                <span id="skip-validate-help">
                  SEARCH 完成后将直接生成 Final，不会运行独立最终评估，也不会产生最终测试或泛化指标。
                </span>
              }
            />
            <Toggle
              label="手动选假设"
              description="由你在对话中选择下一个要验证的假设"
              checked={form.manual_mode}
              onChange={(v) => patch("manual_mode", v)}
            />
          </div>
        </Section>

        <Section id="compute" title="数据与算力" description="数据集位置与实验的运行方式。仅作用于当前会话。">
          <div className={styles.grid}>
            <Field label="数据集根目录" wide hint="指向包含 train.csv 的目录（不是文件本身），应与工作区目录分开。">
              <input
                className="input input--mono"
                type="text"
                placeholder="例如 D:/datasets/tess/task/split"
                value={form.data_root}
                onChange={(e) => patch("data_root", e.target.value)}
              />
            </Field>
            <Field label="运行位置">
              <select className="select" value={form.compute.mode} onChange={(e) => patchCompute("mode", e.target.value as ComputeSettings["mode"])}>
                <option value="local">本机</option>
                <option value="ssh">远程 GPU（SSH）</option>
              </select>
            </Field>
            <Field label="放置策略">
              <select
                className="select"
                value={form.compute.placement}
                onChange={(e) => patchCompute("placement", e.target.value as ComputeSettings["placement"])}
              >
                <option value="pack">集中（pack）</option>
                <option value="spread">分散（spread）</option>
                <option value="homogeneous">同构（homogeneous）</option>
              </select>
            </Field>
            <Field label="无可用 GPU 时">
              <select
                className="select"
                value={form.compute.fallback}
                onChange={(e) => patchCompute("fallback", e.target.value as ComputeSettings["fallback"])}
              >
                <option value="never">排队等待或失败</option>
                <option value="ask">询问我</option>
              </select>
            </Field>
            <Field label="每个实验的 GPU 数" error={errors.gpus_per_experiment}>
              <NumberInput
                value={form.compute.gpus_per_experiment}
                min={1}
                onChange={(v) => patchCompute("gpus_per_experiment", v)}
                invalid={Boolean(errors.gpus_per_experiment)}
              />
            </Field>
            <Field label="排队超时（秒）" hint="留空表示不限" error={errors.queue_timeout_s}>
              <NumberInput
                value={form.compute.queue_timeout_s}
                min={0}
                nullable
                onChange={(v) => patchCompute("queue_timeout_s", v)}
                invalid={Boolean(errors.queue_timeout_s)}
              />
            </Field>
          </div>
          {form.compute.hosts.length > 0 && (
            <div className={styles.hosts}>
              <span className="field__label">远程主机</span>
              {form.compute.hosts.map((host) => (
                <div key={host.name} className={styles.host}>
                  <Icon name="server" size={14} />
                  <span className={styles.hostName}>{host.name}</span>
                  <span className={styles.hostMeta}>
                    {host.ssh} · GPU {host.gpus === "auto" ? "auto" : host.gpus.join(",")} · {host.max_leases} 个租约
                  </span>
                </div>
              ))}
            </div>
          )}
        </Section>

        <Section id="appearance" title="外观">
          <div className="segmented" role="group" aria-label="主题">
            {(
              [
                ["light", "浅色", "sun"],
                ["dark", "深色", "moon"],
                ["system", "跟随系统", "monitor"],
              ] as Array<[ThemePreference, string, IconName]>
            ).map(([value, label, icon]) => (
              <button key={value} type="button" aria-pressed={preference === value} onClick={() => setPreference(value)}>
                <Icon name={icon} size={13} />
                {label}
              </button>
            ))}
          </div>
        </Section>

        <Section id="workspace" title="工作区">
          <div className={styles.workspace}>
            <div className={styles.workspaceInfo}>
              <span className="field__label">当前工作区</span>
              <span className={styles.path}>{settings.project_root || "—"}</span>
              <span className="field__hint">
                运行状态：{phaseLabel(settings.phase)} · {settings.status} · 模型 {settings.model ?? "—"}
              </span>
            </div>
            {onSwitchWorkspace && (
              <button type="button" className="btn" onClick={onSwitchWorkspace}>
                <Icon name="folder" size={14} /> 切换工作区
              </button>
            )}
          </div>
        </Section>
      </div>
    </Dialog>
  );
}

function Section({
  id,
  title,
  description,
  children,
}: {
  id: string;
  title: string;
  description?: string;
  children: ReactNode;
}) {
  return (
    <section className={styles.section} data-section={id} aria-labelledby={`settings-${id}`}>
      <header className={styles.sectionHeader}>
        <h3 id={`settings-${id}`}>{title}</h3>
        {description && <p>{description}</p>}
      </header>
      {children}
    </section>
  );
}

function Field({
  label,
  hint,
  error,
  wide,
  children,
}: {
  label: string;
  hint?: string;
  error?: string;
  wide?: boolean;
  children: ReactNode;
}) {
  return (
    <label className={`field${wide ? ` ${styles.wide}` : ""}`}>
      <span className="field__label">{label}</span>
      {children}
      {error ? <span className="field__error">{error}</span> : hint ? <span className="field__hint">{hint}</span> : null}
    </label>
  );
}

function Toggle({
  label,
  description,
  checked,
  disabled,
  describedBy,
  onChange,
}: {
  label: string;
  description?: ReactNode;
  checked: boolean;
  disabled?: boolean;
  describedBy?: string;
  onChange(value: boolean): void;
}) {
  return (
    <div className={`${styles.toggle}${disabled ? ` ${styles.toggleDisabled}` : ""}`}>
      <div className={styles.toggleText}>
        <span className={styles.toggleLabel}>{label}</span>
        {description && <span className={styles.toggleDesc}>{description}</span>}
      </div>
      <label className="switch">
        <input
          type="checkbox"
          checked={checked}
          disabled={disabled}
          aria-label={label}
          aria-describedby={describedBy}
          onChange={(e) => onChange(e.target.checked)}
        />
        <span className="switch__track" />
      </label>
    </div>
  );
}

/** Number input that tolerates empty text while typing (emits NaN, or null when nullable). */
function NumberInput<T extends number | null>({
  value,
  min,
  step,
  nullable,
  invalid,
  onChange,
}: {
  value: T;
  min?: number;
  step?: string;
  nullable?: boolean;
  invalid?: boolean;
  onChange(value: T): void;
}) {
  const [text, setText] = useState(value === null ? "" : String(value));
  useEffect(() => {
    const parsed = text.trim() === "" ? (nullable ? null : Number.NaN) : Number(text);
    if (!Object.is(parsed, value) && !(Number.isNaN(parsed) && Number.isNaN(value as number))) {
      setText(value === null ? "" : String(value));
    }
    // Only resync when the value changes from outside (load/save).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value]);
  return (
    <input
      className={`input${invalid ? " input--invalid" : ""}`}
      type="number"
      inputMode="decimal"
      min={min}
      step={step}
      value={text}
      onChange={(e) => {
        setText(e.target.value);
        const raw = e.target.value.trim();
        onChange((raw === "" ? (nullable ? null : Number.NaN) : Number(raw)) as T);
      }}
    />
  );
}

export default SettingsPanel;
