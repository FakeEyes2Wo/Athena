import { useEffect, useState } from "react";
import type { ContextPanelKey, ModuleKey } from "../../types/ui";
import { Icon } from "../common/Icon";
import { RELATED_PANELS } from "./navigation";
import { basename } from "../../lib/path";
import { sessionsListFor } from "../../lib/tauri-bridge";
import { loadSessionTitles } from "../../hooks/usePipeline";
import styles from "./ContextSidebar.module.css";

export interface SessionItem {
  id: string;
  title: string;
}

interface WorkspaceGroup {
  root: string;
  name: string;
  isCurrent: boolean;
  sessions: SessionItem[];
}

interface ContextSidebarProps {
  module: ModuleKey;
  currentRoot: string | null;
  recentRoots: string[];
  sessions: SessionItem[];
  currentSessionId: string;
  onSwitchWorkspace(): void;
  onSelectWorkspace(path: string): void;
  onSelectSession(id: string): void;
  onNewSession(): void;
  onDeleteSession(id: string): void;
  onOpenDrawer(panel: ContextPanelKey): void;
}

/** 上下文侧栏：会话模块显示按工作区分组的会话列表，其余模块显示详情视图。 */
export function ContextSidebar({
  module,
  currentRoot,
  recentRoots,
  sessions,
  currentSessionId,
  onSwitchWorkspace,
  onSelectWorkspace,
  onSelectSession,
  onNewSession,
  onDeleteSession,
  onOpenDrawer,
}: ContextSidebarProps) {
  const related = RELATED_PANELS[module];

  return (
    <aside className={styles.sidebar}>
      {module === "session" ? (
        <SessionContext
          currentRoot={currentRoot}
          recentRoots={recentRoots}
          sessions={sessions}
          currentSessionId={currentSessionId}
          onSwitchWorkspace={onSwitchWorkspace}
          onSelectWorkspace={onSelectWorkspace}
          onSelectSession={onSelectSession}
          onNewSession={onNewSession}
          onDeleteSession={onDeleteSession}
        />
      ) : related.length === 0 ? (
        <p className={styles.hint}>无关联详情视图</p>
      ) : (
        <nav aria-label="详情视图">
          <div className={styles.sectionLabel}>详情视图</div>
          <ul className={styles.list}>
            {related.map((item) => (
              <li key={item.key}>
                <button
                  type="button"
                  className={styles.item}
                  onClick={() => onOpenDrawer(item.key)}
                >
                  <Icon name={item.icon} size={15} />
                  <span>{item.label}</span>
                </button>
              </li>
            ))}
          </ul>
        </nav>
      )}
    </aside>
  );
}

function SessionContext({
  currentRoot,
  recentRoots,
  sessions,
  currentSessionId,
  onSwitchWorkspace,
  onSelectWorkspace,
  onSelectSession,
  onNewSession,
  onDeleteSession,
}: {
  currentRoot: string | null;
  recentRoots: string[];
  sessions: SessionItem[];
  currentSessionId: string;
  onSwitchWorkspace(): void;
  onSelectWorkspace(path: string): void;
  onSelectSession(id: string): void;
  onNewSession(): void;
  onDeleteSession(id: string): void;
}) {
  const [otherGroups, setOtherGroups] = useState<WorkspaceGroup[]>([]);

  // 拉取其它工作区的会话（当前工作区的会话由 usePipeline 提供），按工作区分组展示。
  useEffect(() => {
    let cancelled = false;
    const others = recentRoots.filter((root) => root !== currentRoot);
    if (others.length === 0) {
      setOtherGroups([]);
      return;
    }
    Promise.all(
      others.map(async (root) => {
        try {
          const { sessions: ids } = await sessionsListFor(root);
          const titles = loadSessionTitles(root);
          return {
            root,
            name: basename(root),
            isCurrent: false,
            sessions: ids.map((id) => ({ id, title: titles[id] ?? "新会话" })),
          };
        } catch {
          return { root, name: basename(root), isCurrent: false, sessions: [] };
        }
      }),
    ).then((groups) => {
      if (!cancelled) setOtherGroups(groups);
    });
    return () => {
      cancelled = true;
    };
  }, [recentRoots, currentRoot]);

  const currentGroup: WorkspaceGroup = {
    root: currentRoot ?? "",
    name: basename(currentRoot),
    isCurrent: true,
    sessions,
  };
  const groups = [currentGroup, ...otherGroups];

  return (
    <>
      <button className={styles.newSession} onClick={onNewSession}>
        <Icon name="sparkles" size={14} />
        新会话
      </button>
      {groups.map((group) => (
        <nav key={group.root || "current"} aria-label={`${group.name} 会话`} className={styles.workspaceGroup}>
          <button
            type="button"
            className={styles.workspaceHeader}
            onClick={() => {
              if (!group.isCurrent) onSelectWorkspace(group.root);
            }}
            disabled={group.isCurrent}
            title={group.isCurrent ? "当前工作区" : `切换到 ${group.root}`}
          >
            <Icon name="folder" size={14} />
            <span>{group.name}</span>
          </button>
          {group.sessions.length === 0 && <p className={styles.hint}>暂无会话</p>}
          <ul className={styles.list}>
            {group.sessions.map((session) => (
              <li key={session.id} className={styles.sessionRow}>
                <button
                  type="button"
                  className={`${styles.item}${group.isCurrent && session.id === currentSessionId ? ` ${styles["item--active"]}` : ""}`}
                  onClick={() =>
                    group.isCurrent ? onSelectSession(session.id) : onSelectWorkspace(group.root)
                  }
                  aria-current={group.isCurrent && session.id === currentSessionId ? "page" : undefined}
                  title={`${group.name} · ${session.title}`}
                >
                  <span className={styles.sessionTitle}>{session.title}</span>
                </button>
                {group.isCurrent && (
                  <button
                    type="button"
                    className={styles.delete}
                    onClick={() => onDeleteSession(session.id)}
                    aria-label={`删除会话 ${session.title}`}
                    title="删除会话"
                  >
                    <Icon name="trash" size={14} />
                  </button>
                )}
              </li>
            ))}
          </ul>
        </nav>
      ))}
      <button className={styles.switchWorkspace} onClick={onSwitchWorkspace}>
        切换工作区
      </button>
    </>
  );
}
