import { useState } from "react";
import { Icon } from "../common/Icon";
import styles from "./WorkspacePicker.module.css";

interface WorkspacePickerProps {
  /** Active root reported by the backend (null until settings load). */
  currentRoot: string | null;
  /** Recently used roots, most-recent first. */
  recentRoots: string[];
  error: string | null;
  switching: boolean;
  browsing: boolean;
  onSelect(path: string): void;
  onBrowse(): void;
  onContinue(): void;
}

/** Full-screen workspace selection: recent list + path input + continue. */
export function WorkspacePicker({
  currentRoot,
  recentRoots,
  error,
  switching,
  browsing,
  onSelect,
  onBrowse,
  onContinue,
}: WorkspacePickerProps) {
  const [draft, setDraft] = useState("");

  const canContinue = currentRoot != null && !switching;
  const canSelect = draft.trim() !== "" && !switching;

  function submit() {
    if (draft.trim()) onSelect(draft);
  }

  return (
    <div className={styles.picker}>
      <div className={styles.card}>
        <div className={styles.brand}>
          <span className={styles.mark} />
          Athena
        </div>
        <h1 className={styles.title}>选择工作区</h1>
        <p className={styles.subtitle}>
          选择一个项目目录开始研究，或继续使用当前工作区。
        </p>

        {error && <div className="card card--error">{error}</div>}

        <div className={styles.current}>
          <span className={styles.label}>当前工作区</span>
          <span className={styles.path} title={currentRoot ?? ""}>
            <Icon name="folder" size={15} />
            {currentRoot ?? "加载中…"}
          </span>
          <button
            className="btn btn--primary"
            onClick={onContinue}
            disabled={!canContinue}
          >
            继续使用当前
          </button>
        </div>

        {recentRoots.length > 0 && (
          <div className={styles.section}>
            <span className={styles.label}>最近使用</span>
            <ul className={styles.list}>
              {recentRoots.map((root) => (
                <li key={root}>
                  <button
                    className={styles.item}
                    onClick={() => onSelect(root)}
                    disabled={switching}
                    title={root}
                  >
                    <Icon name="folder" size={15} />
                    <span className={styles.itemPath}>{root}</span>
                  </button>
                </li>
              ))}
            </ul>
          </div>
        )}

        <div className={styles.section}>
          <span className={styles.label}>打开其他目录</span>
          <button
            className="btn btn--subtle"
            onClick={onBrowse}
            disabled={browsing || switching}
          >
            {browsing ? "浏览中…" : "浏览目录…"}
          </button>
          <div className={styles.row}>
            <input
              className="input"
              type="text"
              placeholder="输入项目目录的绝对路径…"
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && draft.trim()) submit();
              }}
              disabled={switching}
            />
            <button
              className="btn btn--subtle"
              onClick={submit}
              disabled={!canSelect}
            >
              {switching ? "切换中…" : "打开"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
