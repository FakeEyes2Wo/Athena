import { useEffect, useState, type ReactNode } from "react";
import { settingsGet, type GuiSettings } from "../../lib/tauri-bridge";
import { basename } from "../../lib/path";
import { BrandMark } from "../common/BrandMark";
import { Icon, type IconName } from "../common/Icon";
import styles from "./WelcomeHero.module.css";

/** Example tasks; clicking one fills the composer so it can be edited first. */
export const EXAMPLE_PROMPTS: Array<{ icon: IconName; label: string; prompt: string }> = [
  {
    icon: "target",
    label: "表格二分类",
    prompt: "使用数据目录中的 train.csv 预测 label 列，这是一个二分类任务，以 macro F1 作为主指标。",
  },
  {
    icon: "metrics",
    label: "回归预测",
    prompt: "基于 train.csv 预测 price 列，回归任务，目标是最小化验证集 RMSE。",
  },
  {
    icon: "activity",
    label: "时间序列检测",
    prompt:
      "使用 train.csv 完成 TESS_flare_win120min 二分类研究，优化 macro F1。只运行 1 次 SEARCH，使用 1 个并发 worker。",
  },
  {
    icon: "lightbulb",
    label: "改进现有基线",
    prompt: "在现有基线的基础上尝试特征工程与模型集成，提升验证集 AUC，并说明每次改动的效果。",
  },
];

function greeting(now = new Date()): string {
  const hour = now.getHours();
  if (hour < 5) return "夜深了";
  if (hour < 11) return "早上好";
  if (hour < 13) return "中午好";
  if (hour < 18) return "下午好";
  return "晚上好";
}

interface WelcomeHeroProps {
  /** The composer, rendered in the center of the welcome screen. */
  composer: ReactNode;
  onExample(prompt: string): void;
  onOpenSettings?(): void;
  /** Changes whenever settings were saved, to refresh the readiness hints. */
  settingsVersion?: number;
}

/** Empty-conversation landing: greeting, centered composer, examples, readiness. */
export function WelcomeHero({ composer, onExample, onOpenSettings, settingsVersion = 0 }: WelcomeHeroProps) {
  const [settings, setSettings] = useState<GuiSettings | null>(null);

  useEffect(() => {
    let cancelled = false;
    settingsGet()
      .then((next) => {
        if (!cancelled && next && typeof next === "object") setSettings(next);
      })
      .catch(() => {
        // Readiness hints are optional; the composer stays usable without them.
      });
    return () => {
      cancelled = true;
    };
  }, [settingsVersion]);

  return (
    <div className={styles.hero}>
      <div className={styles.inner}>
        <div className={styles.heading}>
          <BrandMark size={34} />
          <h2 className={styles.title}>{greeting()}，今天想研究什么？</h2>
        </div>
        <p className={styles.subtitle}>
          描述数据、预测目标与评价指标。Athena 会先和你确认任务理解，再自动提出假设、运行实验并比较结果。
        </p>

        <div className={styles.composer}>{composer}</div>

        <div className={styles.examples}>
          {EXAMPLE_PROMPTS.map((example) => (
            <button
              key={example.label}
              type="button"
              className="chip"
              onClick={() => onExample(example.prompt)}
              title={example.prompt}
            >
              <Icon name={example.icon} size={14} />
              {example.label}
            </button>
          ))}
        </div>

        {settings && <Readiness settings={settings} onOpenSettings={onOpenSettings} />}
      </div>
    </div>
  );
}

function Readiness({ settings, onOpenSettings }: { settings: GuiSettings; onOpenSettings?(): void }) {
  const connection = settings.model_connection;
  const hasKey = Boolean(connection?.llm_api_key);
  const modelName = connection?.model_name || settings.model || "未指定模型";
  const items: Array<{ key: string; icon: IconName; text: string; ok: boolean; hint: string }> = [
    {
      key: "model",
      icon: "key",
      text: hasKey ? `${connection?.provider ?? "LLM"} · ${modelName}` : "尚未配置模型 API Key",
      ok: hasKey,
      hint: "模型连接",
    },
    {
      key: "data",
      icon: "database",
      text: settings.data_root ? `数据目录 · ${basename(settings.data_root)}` : "未设置数据集根目录",
      ok: Boolean(settings.data_root),
      hint: settings.data_root ?? "数据集根目录（包含 train.csv 的目录）",
    },
    {
      key: "search",
      icon: "sliders",
      text: `搜索上限 ${settings.search_limit} 次 · 并发 ${settings.concurrency}`,
      ok: true,
      hint: "运行参数",
    },
  ];

  return (
    <div className={styles.readiness} aria-label="运行准备情况">
      {items.map((item) => (
        <button
          key={item.key}
          type="button"
          className={`${styles.ready}${item.ok ? "" : ` ${styles.readyWarn}`}`}
          onClick={onOpenSettings}
          title={`${item.hint} — 点击打开设置`}
          disabled={!onOpenSettings}
        >
          <Icon name={item.ok ? item.icon : "warning"} size={13} />
          {item.text}
        </button>
      ))}
    </div>
  );
}
