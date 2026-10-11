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
import { Button, Checkbox, Dialog, Flash, FormControl, Heading, IconButton, Select, TextInput } from "@primer/react";
import { XIcon } from "@primer/octicons-react";
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

/** 运行设置弹窗（Primer Dialog）：项目目录、模型连接、运行参数、算力与开关。 */
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
    <Dialog
      width="xlarge"
      onClose={onClose}
      renderHeader={({ dialogLabelId, dialogDescriptionId }) => (
        <Dialog.Header className={styles.header}>
          <div className={styles.headerText}>
            <Dialog.Title id={dialogLabelId}>运行设置</Dialog.Title>
            <Dialog.Subtitle id={dialogDescriptionId}>配置 Athena 运行参数与项目目录</Dialog.Subtitle>
          </div>
          <IconButton icon={XIcon} variant="invisible" aria-label="关闭设置" onClick={onClose} />
        </Dialog.Header>
      )}
      footerButtons={[
        { content: "刷新", buttonType: "default", onClick: () => void refresh() },
        {
          content: saving ? "保存中…" : "保存",
          buttonType: "primary",
          onClick: () => void save(),
          disabled: saving,
        },
      ]}
    >
      {error && (
        <Flash variant="danger" className={styles.flash}>
          {error}
        </Flash>
      )}
      {saved && !error && (
        <Flash variant="success" className={styles.flash}>
          设置已保存
        </Flash>
      )}

      <section className={styles.section}>
        <Heading as="h3" variant="small">项目</Heading>
        <div className={styles.projectRow}>
          <TextInput
            block
            aria-label="项目目录"
            placeholder="选择项目目录…"
            value={projectRoot}
            onChange={(e) => setProjectRootInput(e.target.value)}
          />
          <Button variant="primary" onClick={() => void switchProject()} disabled={switching || !projectRoot.trim()}>
            {switching ? "切换中…" : "切换"}
          </Button>
        </div>
      </section>

      <section className={styles.section}>
        <Heading as="h3" variant="small">模型连接</Heading>
        <div className={styles.grid}>
          <FormControl>
            <FormControl.Label>PROVIDER</FormControl.Label>
            <Select
              block
              value={form.model_connection.provider}
              onChange={(e) => patchModelConnection("provider", e.target.value)}
            >
              <Select.Option value="deepseek">deepseek</Select.Option>
              <Select.Option value="openai">openai</Select.Option>
              <Select.Option value="qwen">qwen</Select.Option>
            </Select>
          </FormControl>

          <FormControl>
            <FormControl.Label>MODEL_NAME</FormControl.Label>
            <TextInput
              block
              placeholder="deepseek-v4-flash"
              value={form.model_connection.model_name}
              onChange={(e) => patchModelConnection("model_name", e.target.value)}
            />
          </FormControl>

          <FormControl className={styles.full}>
            <FormControl.Label>BASE_URL</FormControl.Label>
            <TextInput
              block
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
          </FormControl>

          <FormControl className={styles.full}>
            <FormControl.Label>LLM_API_KEY</FormControl.Label>
            <TextInput
              block
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
            <FormControl.Caption>密钥只写回 .env，界面不回显明文；留空表示不修改。</FormControl.Caption>
          </FormControl>
        </div>
      </section>

      <section className={styles.section}>
        <Heading as="h3" variant="small">运行参数</Heading>
        <div className={styles.grid}>
          <FormControl>
            <FormControl.Label>并发度</FormControl.Label>
            <TextInput
              block
              type="number"
              min={1}
              value={String(form.concurrency)}
              onChange={(e) => patchField("concurrency", Number(e.target.value))}
            />
          </FormControl>

          <FormControl>
            <FormControl.Label>搜索上限</FormControl.Label>
            <TextInput
              block
              type="number"
              min={0}
              value={String(form.search_limit)}
              onChange={(e) => patchField("search_limit", Number(e.target.value))}
            />
          </FormControl>

          <FormControl>
            <FormControl.Label>Ideator 机制</FormControl.Label>
            <Select block value={form.ideation} onChange={(e) => patchField("ideation", e.target.value)}>
              <Select.Option value="ideageneration">ideageneration · 门禁生成</Select.Option>
              <Select.Option value="baseline">baseline · 消融对照</Select.Option>
              <Select.Option value="debate">debate · 辩论式</Select.Option>
            </Select>
          </FormControl>

          <FormControl>
            <FormControl.Label>优化方向</FormControl.Label>
            <Select block value={form.direction} onChange={(e) => patchField("direction", e.target.value)}>
              <Select.Option value="maximize">maximize</Select.Option>
              <Select.Option value="minimize">minimize</Select.Option>
            </Select>
          </FormControl>

          <FormControl>
            <FormControl.Label>容忍度</FormControl.Label>
            <TextInput
              block
              type="number"
              min={0}
              step="0.01"
              value={String(form.tolerance)}
              onChange={(e) => patchField("tolerance", Number(e.target.value))}
            />
          </FormControl>

          <FormControl>
            <FormControl.Label>实验超时（秒）</FormControl.Label>
            <TextInput
              block
              type="number"
              min={1}
              value={String(form.experiment_timeout_s)}
              onChange={(e) => patchField("experiment_timeout_s", Number(e.target.value))}
            />
          </FormControl>
        </div>
      </section>

      <section className={styles.section}>
        <Heading as="h3" variant="small">算力 / Compute</Heading>
        <div className={styles.grid}>
          <FormControl>
            <FormControl.Label>模式</FormControl.Label>
            <Select block value={form.compute.mode} onChange={(e) => patchCompute("mode", e.target.value)}>
              <Select.Option value="local">local · 本机</Select.Option>
              <Select.Option value="ssh">ssh · 远程 GPU</Select.Option>
            </Select>
          </FormControl>

          <FormControl>
            <FormControl.Label>放置策略</FormControl.Label>
            <Select block value={form.compute.placement} onChange={(e) => patchCompute("placement", e.target.value)}>
              <Select.Option value="pack">pack</Select.Option>
              <Select.Option value="spread">spread</Select.Option>
              <Select.Option value="homogeneous">homogeneous</Select.Option>
            </Select>
          </FormControl>

          <FormControl>
            <FormControl.Label>拿不到卡时</FormControl.Label>
            <Select block value={form.compute.fallback} onChange={(e) => patchCompute("fallback", e.target.value)}>
              <Select.Option value="never">never · 排队/失败</Select.Option>
              <Select.Option value="ask">ask · 询问</Select.Option>
            </Select>
          </FormControl>

          <FormControl>
            <FormControl.Label>每实验 GPU 数</FormControl.Label>
            <TextInput
              block
              type="number"
              min={1}
              value={String(form.compute.gpus_per_experiment)}
              onChange={(e) => patchCompute("gpus_per_experiment", Number(e.target.value))}
            />
          </FormControl>

          <FormControl>
            <FormControl.Label>排队超时（秒）</FormControl.Label>
            <TextInput
              block
              type="number"
              min={0}
              value={form.compute.queue_timeout_s == null ? "" : String(form.compute.queue_timeout_s)}
              onChange={(e) => {
                const value = e.target.value;
                patchCompute("queue_timeout_s", value === "" ? null : Number(value));
              }}
            />
          </FormControl>

          <FormControl className={styles.full}>
            <FormControl.Label>数据集根目录</FormControl.Label>
            <TextInput
              block
              placeholder="留空表示不设置"
              value={form.data_root ?? ""}
              onChange={(e) =>
                setForm((prev) => ({
                  ...prev,
                  data_root: e.target.value.trim() || null,
                }))
              }
            />
          </FormControl>
        </div>
        {form.compute.hosts.length > 0 && (
          <div className={styles.grid}>
            {form.compute.hosts.map((host) => (
              <ReadOnlyField
                key={host.name}
                label={host.name}
                value={`${host.ssh} · ${host.gpus === "auto" ? "auto" : host.gpus.join(",")} · ${host.max_leases} leases`}
              />
            ))}
          </div>
        )}
      </section>

      <section className={styles.section}>
        <Heading as="h3" variant="small">开关</Heading>
        <div className={styles.switches}>
          <FormControl disabled={form.skip_validate}>
            <Checkbox
              checked={form.auto_validate}
              onChange={(e) => patchField("auto_validate", e.target.checked)}
            />
            <FormControl.Label>自动验证</FormControl.Label>
          </FormControl>

          <FormControl id="skip-validate">
            <Checkbox
              checked={form.skip_validate}
              onChange={(e) => patchField("skip_validate", e.target.checked)}
            />
            <FormControl.Label>跳过 VALIDATE</FormControl.Label>
            <FormControl.Caption>
              SEARCH 完成后将直接生成 Final，不会运行独立最终评估，也不会产生最终测试或泛化指标。
            </FormControl.Caption>
          </FormControl>

          <FormControl>
            <Checkbox
              checked={form.manual_mode}
              onChange={(e) => patchField("manual_mode", e.target.checked)}
            />
            <FormControl.Label>手动选假设</FormControl.Label>
          </FormControl>
        </div>
      </section>

      <section className={styles.section}>
        <Heading as="h3" variant="small">只读状态</Heading>
        <div className={styles.grid}>
          <ReadOnlyField label="模型" value={settings.model ?? "—"} />
          <ReadOnlyField label="阶段" value={settings.phase} />
          <ReadOnlyField label="状态" value={settings.status} />
        </div>
      </section>
    </Dialog>
  );
}

function ReadOnlyField({ label, value }: { label: string; value: string }) {
  return (
    <FormControl>
      <FormControl.Label>{label}</FormControl.Label>
      <TextInput block readOnly value={value} />
    </FormControl>
  );
}

export default SettingsPanel;
