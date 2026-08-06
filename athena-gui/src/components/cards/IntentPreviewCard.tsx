import type { TaskPreview } from "../../lib/tauri-bridge";

interface IntentPreviewCardProps {
  preview: TaskPreview;
  onConfirm(): Promise<void>;
}

/** Shows a parsed task intent with a "confirm and start" action button. */
export function IntentPreviewCard({ preview, onConfirm }: IntentPreviewCardProps) {
  return (
    <section className="card card--action">
      <h3 className="card__title">意图预览</h3>
      <p className="card__body">
        {`任务类型: ${preview.task_type} · 主指标: ${preview.primary_metric}`}
        {preview.needs_configuration ? " — 请在启动搜索前确认配置" : ""}
      </p>
      <button className="card__action" onClick={() => void onConfirm()}>
        确认并启动
      </button>
    </section>
  );
}
