import { useState } from "react";
import { isTauri } from "@tauri-apps/api/core";
import { basename } from "../../lib/path";
import { errorMessage } from "../../lib/errors";
import { BrandMark } from "../common/BrandMark";
import { Icon } from "../common/Icon";
import styles from "./WorkspacePicker.module.css";
import { listWorkspaceDirectories, type WorkspaceDirectoryListing } from "../../lib/workspaceDialog";

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

/** Full-screen workspace selection: current, recent, browse or type a path. */
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
      setBrowserError(errorMessage(err));
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
          <BrandMark size={30} withName />
        </div>
        <h1 className={styles.title}>选择工作区</h1>
        <p className={styles.subtitle}>
          工作区是 Athena 保存会话、实验代码与结果的项目目录。请选择一个独立目录，不要直接使用数据集目录。
        </p>

        {error && (
          <div className="callout callout--error" role="alert">
            <Icon name="alert" size={15} />
            <div className="callout__body">{error}</div>
          </div>
        )}

        {switching && (
          <div className={styles.switching} role="status">
            <Icon name="loader" size={14} className="icon--spin" /> 正在打开工作区…
          </div>
        )}

        <section className={styles.section}>
          <span className={styles.label}>当前工作区</span>
          <div className={styles.current}>
            <span className={styles.folderIcon}>
              <Icon name="folder" size={17} />
            </span>
            <span className={styles.currentText}>
              <span className={styles.currentName}>{currentRoot ? basename(currentRoot) : "尚未加载"}</span>
              <span className={styles.currentPath} title={currentRoot ?? ""}>
                {currentRoot ?? "等待后端返回当前目录…"}
              </span>
            </span>
            <button className="btn btn--primary" onClick={onContinue} disabled={!canContinue}>
              继续使用当前
            </button>
          </div>
        </section>

        {recentRoots.length > 0 && (
          <section className={styles.section}>
            <span className={styles.label}>最近使用</span>
            <ul className={styles.list}>
              {recentRoots.map((root) => (
                <li key={root}>
                  <button className={styles.item} onClick={() => onSelect(root)} title={root} aria-label={root}>
                    <Icon name="folder" size={15} />
                    <span className={styles.itemName}>{basename(root)}</span>
                    <span className={styles.itemPath}>{root}</span>
                    {root === currentRoot && <span className="badge badge--accent">当前</span>}
                    <Icon name="arrowRight" size={14} className={styles.itemArrow} />
                  </button>
                </li>
              ))}
            </ul>
          </section>
        )}

        <section className={styles.section}>
          <span className={styles.label}>打开其他目录</span>
          <div className={styles.row}>
            <input
              className="input input--mono"
              type="text"
              placeholder="输入项目目录的绝对路径…"
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && draft.trim()) submit();
              }}
            />
            <button className="btn" onClick={submit} disabled={!canSelect}>
              打开
            </button>
          </div>
          <div className={styles.browseRow}>
            <button className="btn btn--ghost btn--sm" onClick={handleBrowse} disabled={browsing || browserLoading}>
              <Icon name="search" size={13} />
              {browsing || browserLoading ? "浏览中…" : "浏览目录…"}
            </button>
            <span className={styles.hint}>目录不存在时会自动创建</span>
          </div>
        </section>

        {browserOpen && (
          <div className={styles.browser} role="dialog" aria-label="浏览工作区目录">
            <div className={styles.browserHeader}>
              <span className={styles.label}>选择工作区目录</span>
              <button className="btn btn--ghost btn--sm" onClick={() => setBrowserOpen(false)} disabled={browserLoading}>
                取消
              </button>
            </div>
            {browserError && (
              <div className="callout callout--error" role="alert">
                <Icon name="alert" size={15} />
                <div className="callout__body">{browserError}</div>
              </div>
            )}
            {listing && (
              <>
                <div className={styles.browserPath} title={listing.path}>
                  <Icon name="folder" size={15} />
                  <span>{listing.path}</span>
                </div>
                <div className={styles.browserActions}>
                  <button
                    className="btn btn--sm"
                    onClick={() => void openBrowser(listing.parent)}
                    disabled={!listing.parent || browserLoading}
                  >
                    <Icon name="arrowLeft" size={13} /> 上一级
                  </button>
                  <button
                    className="btn btn--primary btn--sm"
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
                        className={styles.browserItem}
                        onClick={() => void openBrowser(directory.path)}
                        disabled={browserLoading}
                      >
                        <Icon name="folder" size={15} />
                        <span>{directory.name}</span>
                      </button>
                    </li>
                  ))}
                  {listing.directories.length === 0 && <li className={styles.browserEmpty}>此目录没有子目录。</li>}
                </ul>
              </>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
