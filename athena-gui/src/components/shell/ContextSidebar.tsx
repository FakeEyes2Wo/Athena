import type { ContextPanelKey, ModuleKey } from "../../types/ui";
import { Icon } from "../common/Icon";
import { RELATED_PANELS } from "./navigation";
import { basename } from "../../lib/path";
import styles from "./ContextSidebar.module.css";

export interface SessionItem {
  id: string;
  title: string;
}

interface ContextSidebarProps {
  module: ModuleKey;
  currentRoot: string | null;
  sessions: SessionItem[];
  currentSessionId: string;
  onSwitchWorkspace(): void;
  onSelectSession(id: string): void;
  onNewSession(): void;
  onDeleteSession(id: string): void;
  onOpenDrawer(panel: ContextPanelKey): void;
}

/** 上下文侧栏：会话模块显示工作区/新会话/会话列表，其余模块显示详情视图。 */
export function ContextSidebar({
  module,
  currentRoot,
  sessions,
  currentSessionId,
  onSwitchWorkspace,
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
          sessions={sessions}
          currentSessionId={currentSessionId}
          onSwitchWorkspace={onSwitchWorkspace}
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
  sessions,
  currentSessionId,
  onSwitchWorkspace,
  onSelectSession,
  onNewSession,
  onDeleteSession,
}: {
  currentRoot: string | null;
  sessions: SessionItem[];
  currentSessionId: string;
  onSwitchWorkspace(): void;
  onSelectSession(id: string): void;
  onNewSession(): void;
  onDeleteSession(id: string): void;
}) {
  return (
    <>
      <div className={styles.workspace}>
        <div className={styles.sectionLabel}>工作区</div>
        <div className={styles.workspaceName} title={currentRoot ?? ""}>
          <Icon name="folder" size={14} />
          <span>{basename(currentRoot)}</span>
        </div>
        <button className="btn btn--ghost btn--sm" onClick={onSwitchWorkspace}>
          切换工作区
        </button>
      </div>
      <button className={styles.newSession} onClick={onNewSession}>
        <Icon name="sparkles" size={14} />
        新会话
      </button>
      {sessions.length > 0 && (
        <nav aria-label="会话列表">
          <div className={styles.sectionLabel}>会话</div>
          <ul className={styles.list}>
            {sessions.map((session) => (
              <li key={session.id} className={styles.sessionRow}>
                <button
                  type="button"
                  className={`${styles.item}${session.id === currentSessionId ? ` ${styles["item--active"]}` : ""}`}
                  onClick={() => onSelectSession(session.id)}
                  aria-current={session.id === currentSessionId ? "page" : undefined}
                  title={session.title}
                >
                  <Icon name="chat" size={15} />
                  <span>{session.title}</span>
                </button>
                {session.id !== "default" && (
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
      )}
    </>
  );
}
