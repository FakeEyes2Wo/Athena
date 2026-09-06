import { useState } from "react";
import { isTauri } from "@tauri-apps/api/core";
import { Icon } from "../common/Icon";
import styles from "./WorkspacePicker.module.css";
import {
  listWorkspaceDirectories,
  type WorkspaceDirectoryListing,
} from "../../lib/workspaceDialog";

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
  const [browserOpen, setBrowserOpen] = useState(false);
  const [browserLoading, setBrowserLoading] = useState(false);
  const [browserError, setBrowserError] = useState<string | null>(null);
  const [listing, setListing] = useState<WorkspaceDirectoryListing | null>(null);

  const canContinue = currentRoot != null && !switching;
  const canSelect = draft.trim() !== "";

  function submit() {
    if (draft.trim()) onSelect(draft);
  }

  async function openBrowser(path?: string | null) {
    setBrowserLoading(true);
    setBrowserError(null);
    try {
      setListing(await listWorkspaceDirectories(path));
    } catch (err) {
      setBrowserError(err instanceof Error ? err.message : String(err));
    } finally {
      setBrowserLoading(false);
    }
  }

  function handleBrowse() {
    if (isTauri()) {
      onBrowse();
      return;
    }
    setBrowserOpen(true);
    void openBrowser(currentRoot);
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
            onClick={handleBrowse}
            disabled={browsing || browserLoading}
          >
            {browsing || browserLoading ? "浏览中…" : "浏览目录…"}
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
            />
            <button
              className="btn btn--subtle"
              onClick={submit}
              disabled={!canSelect}
            >
              打开
            </button>
          </div>
        </div>

        {browserOpen && (
          <div className={styles.browser} role="dialog" aria-label="浏览工作区目录">
            <div className={styles.browserHeader}>
              <span className={styles.label}>选择工作区目录</span>
              <button
                className="btn btn--subtle"
                onClick={() => setBrowserOpen(false)}
                disabled={browserLoading}
              >
                取消
              </button>
            </div>
            {browserError && <div className="card card--error">{browserError}</div>}
            {listing && (
              <>
                <div className={styles.browserPath} title={listing.path}>
                  <Icon name="folder" size={15} />
                  {listing.path}
                </div>
                <div className={styles.browserActions}>
                  <button
                    className="btn btn--subtle"
                    onClick={() => void openBrowser(listing.parent)}
                    disabled={!listing.parent || browserLoading}
                  >
                    上一级
                  </button>
                  <button
                    className="btn btn--primary"
                    onClick={() => {
                      setBrowserOpen(false);
                      onSelect(listing.path);
                    }}
                    disabled={browserLoading}
                  >
                    选择此目录
                  </button>
                </div>
                <ul className={styles.browserList}>
                  {listing.directories.map((directory) => (
                    <li key={directory.path}>
                      <button
                        className={styles.item}
                        onClick={() => void openBrowser(directory.path)}
                        disabled={browserLoading}
                      >
                        <Icon name="folder" size={15} />
                        <span className={styles.itemPath}>{directory.name}</span>
                      </button>
                    </li>
                  ))}
                  {listing.directories.length === 0 && (
                    <li className={styles.browserEmpty}>此目录没有子目录。</li>
                  )}
                </ul>
              </>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
