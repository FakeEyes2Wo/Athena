import type { ContextPanelKey, ModuleKey } from "../../types/ui";
import { Icon } from "../common/Icon";
import { RELATED_PANELS } from "./navigation";
import { basename } from "../../lib/path";
import { loadWorkspaceSessions } from "../../lib/workspaceStorage";
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

export interface ContextSidebarProps {
  module: ModuleKey;
  currentRoot: string | null;
  recentRoots: string[];
  switching: boolean;
  sessions: SessionItem[];
  currentSessionId: string;
  onSwitchWorkspace(): void;
  onSelectWorkspace(path: string, sessionId?: string): void;
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
  onSelectWorkspace(path: string, sessionId?: string): void;
  onSelectSession(id: string): void;
  onNewSession(): void;
  onDeleteSession(id: string): void;
}) {
  const otherGroups = recentRoots
    .filter((root) => root !== currentRoot)
    .map((root): WorkspaceGroup => ({
      root,
      name: basename(root),
      isCurrent: false,
      sessions: loadWorkspaceSessions(root),
    }));

  const orderedRoots = currentRoot && !recentRoots.includes(currentRoot)
    ? [...recentRoots, currentRoot]
    : recentRoots;
  const otherGroupsByRoot = new Map(otherGroups.map((group) => [group.root, group]));
  const groups = orderedRoots.flatMap((root): WorkspaceGroup[] => {
    if (root === currentRoot) {
      return [{ root, name: basename(root), isCurrent: true, sessions }];
    }
    const group = otherGroupsByRoot.get(root);
    return group ? [group] : [];
  });

  return (
    <div className={styles.sessionContext}>
      <button className={styles.newSession} onClick={onNewSession}>
        <Icon name="sparkles" size={14} />
        新会话
      </button>
      <div className={styles.workspaceScroll} data-testid="workspace-scroll">
        {groups.map((group) => (
          <nav key={group.root} aria-label={`${group.name} 会话`} className={styles.workspaceGroup}>
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
                      group.isCurrent ? onSelectSession(session.id) : onSelectWorkspace(group.root, session.id)
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
      </div>
      <footer className={styles.sidebarFooter}>
        <button className={styles.switchWorkspace} onClick={onSwitchWorkspace}>
          切换工作区
        </button>
      </footer>
    </div>
  );
}
