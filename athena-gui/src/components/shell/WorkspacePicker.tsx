import { useState } from "react";
import { isTauri } from "@tauri-apps/api/core";
import { Button, Flash, Heading, TextInput } from "@primer/react";
import { FileDirectoryIcon } from "@primer/octicons-react";
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
          <span className={styles.mark} aria-hidden="true">
            <Icon name="sparkles" size={16} />
          </span>
          Athena
        </div>
        <div className={styles.heading}>
          <Heading as="h1" variant="medium">选择工作区</Heading>
          <p className={styles.subtitle}>选择一个项目目录开始研究，或继续使用当前工作区。</p>
        </div>

        {error && <Flash variant="danger">{error}</Flash>}

        <section className={styles.current}>
          <span className={styles.label}>当前工作区</span>
          <span className={styles.path} title={currentRoot ?? ""}>
            <FileDirectoryIcon size={16} />
            {currentRoot ?? "加载中…"}
          </span>
          <Button variant="primary" onClick={onContinue} disabled={!canContinue}>
            继续使用当前
          </Button>
        </section>

        {recentRoots.length > 0 && (
          <section className={styles.section}>
            <span className={styles.label}>最近使用</span>
            <ul className={styles.list}>
              {recentRoots.map((root) => (
                <li key={root}>
                  <Button
                    variant="invisible"
                    block
                    alignContent="start"
                    leadingVisual={FileDirectoryIcon}
                    className={styles.item}
                    onClick={() => onSelect(root)}
                    title={root}
                  >
                    {root}
                  </Button>
                </li>
              ))}
            </ul>
          </section>
        )}

        <section className={styles.section}>
          <span className={styles.label}>打开其他目录</span>
          <Button onClick={handleBrowse} disabled={browsing || browserLoading}>
            {browsing || browserLoading ? "浏览中…" : "浏览目录…"}
          </Button>
          <div className={styles.row}>
            <TextInput
              block
              aria-label="项目目录路径"
              placeholder="输入项目目录的绝对路径…"
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && draft.trim()) submit();
              }}
            />
            <Button onClick={submit} disabled={!canSelect}>
              打开
            </Button>
          </div>
        </section>

        {browserOpen && (
          <div className={styles.browser} role="dialog" aria-label="浏览工作区目录">
            <div className={styles.browserHeader}>
              <span className={styles.label}>选择工作区目录</span>
              <Button size="small" onClick={() => setBrowserOpen(false)} disabled={browserLoading}>
                取消
              </Button>
            </div>
            {browserError && <Flash variant="danger">{browserError}</Flash>}
            {listing && (
              <>
                <div className={styles.path} title={listing.path}>
                  <FileDirectoryIcon size={16} />
                  {listing.path}
                </div>
                <div className={styles.browserActions}>
                  <Button
                    size="small"
                    onClick={() => void openBrowser(listing.parent)}
                    disabled={!listing.parent || browserLoading}
                  >
                    上一级
                  </Button>
                  <Button
                    size="small"
                    variant="primary"
                    onClick={() => {
                      setBrowserOpen(false);
                      onSelect(listing.path);
                    }}
                    disabled={browserLoading}
                  >
                    选择此目录
                  </Button>
                </div>
                <ul className={`${styles.list} ${styles.browserList}`}>
                  {listing.directories.map((directory) => (
                    <li key={directory.path}>
                      <Button
                        variant="invisible"
                        block
                        alignContent="start"
                        leadingVisual={FileDirectoryIcon}
                        className={styles.item}
                        onClick={() => void openBrowser(directory.path)}
                        disabled={browserLoading}
                      >
                        {directory.name}
                      </Button>
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
