import { useMemo, useState } from "react";
import type { ModuleKey, PipelineStatus } from "../../types/ui";
import { basename } from "../../lib/path";
import { loadWorkspaceSessions } from "../../lib/workspaceStorage";
import { BrandMark } from "../common/BrandMark";
import { Icon } from "../common/Icon";
import { MenuButton } from "../common/Menu";
import { ThemeToggle } from "./ThemeToggle";
import { MODULES } from "./navigation";
import styles from "./Sidebar.module.css";

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

export interface SidebarProps {
  collapsed: boolean;
  onToggleCollapsed(): void;
  module: ModuleKey;
  onSelectModule(module: ModuleKey): void;
  currentRoot: string | null;
  recentRoots: string[];
  sessions: SessionItem[];
  currentSessionId: string;
  /** Live status of the current session's run (drives the list indicator). */
  runStatus?: PipelineStatus;
  /** A question is waiting for the user (badge on the conversation entry). */
  attention?: boolean;
  onSwitchWorkspace(): void;
  onSelectWorkspace(path: string, sessionId?: string): void;
  onSelectSession(id: string): void;
  onNewSession(): void;
  onDeleteSession(id: string): void;
  onRenameSession?(id: string, title: string): void;
  onOpenSettings(): void;
}

/** Claude-style sidebar: brand, new session, views, sessions by workspace, footer. */
export function Sidebar(props: SidebarProps) {
  const { collapsed, onToggleCollapsed, module, onSelectModule, attention } = props;

  if (collapsed) {
    return (
      <aside className={`${styles.sidebar} ${styles.collapsed}`} aria-label="侧栏">
        <div className={styles.railTop}>
          <button
            type="button"
            className="icon-btn"
            onClick={onToggleCollapsed}
            aria-label="展开侧栏"
            title="展开侧栏"
          >
            <Icon name="panelLeft" size={18} />
          </button>
          <button
            type="button"
            className={`icon-btn ${styles.railNew}`}
            onClick={props.onNewSession}
            aria-label="新会话"
            title="新会话"
          >
            <Icon name="newChat" size={17} />
          </button>
        </div>
        <nav className={styles.railNav} aria-label="主导航">
          {MODULES.map((item) => (
            <button
              key={item.key}
              type="button"
              className={`icon-btn ${styles.railItem}`}
              aria-label={item.label}
              title={item.label}
              aria-current={module === item.key ? "page" : undefined}
              onClick={() => onSelectModule(item.key)}
            >
              <Icon name={item.icon} size={18} />
              {item.key === "session" && attention && <span className={styles.railDot} aria-hidden />}
            </button>
          ))}
        </nav>
        <div className={styles.railBottom}>
          <button
            type="button"
            className="icon-btn"
            onClick={props.onOpenSettings}
            aria-label="设置"
            title="设置"
          >
            <Icon name="settings" size={18} />
          </button>
        </div>
      </aside>
    );
  }

  return (
    <aside className={styles.sidebar} aria-label="侧栏">
      <div className={styles.top}>
        <BrandMark size={24} withName />
        <button
          type="button"
          className="icon-btn"
          onClick={onToggleCollapsed}
          aria-label="折叠侧栏"
          title="折叠侧栏"
        >
          <Icon name="panelLeft" size={18} />
        </button>
      </div>

      <button type="button" className={styles.newSession} onClick={props.onNewSession}>
        <span className={styles.newSessionIcon}>
          <Icon name="plus" size={15} strokeWidth={2.2} />
        </span>
        新会话
      </button>

      <nav className={styles.nav} aria-label="主导航">
        {MODULES.map((item) => (
          <button
            key={item.key}
            type="button"
            className={styles.navItem}
            aria-current={module === item.key ? "page" : undefined}
            onClick={() => onSelectModule(item.key)}
          >
            <Icon name={item.icon} size={17} />
            <span>{item.label}</span>
            {item.key === "session" && attention && (
              <span className={styles.attention} title="有问题等待你回答" aria-hidden />
            )}
          </button>
        ))}
      </nav>

      <SessionGroups {...props} />

      <footer className={styles.footer}>
        <div className={styles.workspace} title={props.currentRoot ?? ""}>
          <Icon name="folder" size={16} />
          <div className={styles.workspaceText}>
            <span className={styles.workspaceLabel}>工作区</span>
            <span className={styles.workspaceName}>{basename(props.currentRoot)}</span>
          </div>
          <button
            type="button"
            className="icon-btn icon-btn--sm"
            onClick={props.onSwitchWorkspace}
            aria-label="切换工作区"
            title="切换工作区"
          >
            <Icon name="arrowRight" size={15} />
          </button>
        </div>
        <div className={styles.footerRow}>
          <button type="button" className={styles.footerButton} onClick={props.onOpenSettings}>
            <Icon name="settings" size={16} />
            设置
          </button>
          <ThemeToggle />
        </div>
      </footer>
    </aside>
  );
}

function SessionGroups({
  currentRoot,
  recentRoots,
  sessions,
  currentSessionId,
  runStatus,
  onSelectWorkspace,
  onSelectSession,
  onDeleteSession,
  onRenameSession,
}: SidebarProps) {
  const otherGroups = useMemo(
    () =>
      recentRoots
        .filter((root) => root !== currentRoot)
        .map(
          (root): WorkspaceGroup => ({
            root,
            name: basename(root),
            isCurrent: false,
            sessions: loadWorkspaceSessions(root),
          }),
        ),
    [currentRoot, recentRoots],
  );

  const orderedRoots =
    currentRoot && !recentRoots.includes(currentRoot) ? [...recentRoots, currentRoot] : recentRoots;
  const otherGroupsByRoot = new Map(otherGroups.map((group) => [group.root, group]));
  const groups = orderedRoots.flatMap((root): WorkspaceGroup[] => {
    if (root === currentRoot) {
      return [{ root, name: basename(root), isCurrent: true, sessions }];
    }
    const group = otherGroupsByRoot.get(root);
    return group ? [group] : [];
  });
  // Without any remembered workspace, still show the current session list.
  if (groups.length === 0) {
    groups.push({ root: currentRoot ?? "", name: basename(currentRoot), isCurrent: true, sessions });
  }

  return (
    <div className={styles.sessions} data-testid="workspace-scroll">
      <div className={styles.sectionLabel}>会话</div>
      {groups.map((group) => (
        <nav key={group.root} aria-label={`${group.name} 会话`} className={styles.group}>
          <button
            type="button"
            className={styles.groupHeader}
            onClick={() => {
              if (!group.isCurrent) onSelectWorkspace(group.root);
            }}
            disabled={group.isCurrent}
            title={group.isCurrent ? `当前工作区：${group.root}` : `切换到 ${group.root}`}
          >
            <Icon name="folder" size={13} />
            <span>{group.name}</span>
          </button>
          {group.sessions.length === 0 && <p className={styles.hint}>暂无会话</p>}
          <ul className={styles.list}>
            {group.sessions.map((session) => (
              <SessionRow
                key={session.id}
                session={session}
                groupName={group.name}
                active={group.isCurrent && session.id === currentSessionId}
                running={
                  group.isCurrent &&
                  session.id === currentSessionId &&
                  (runStatus === "running" || runStatus === "paused")
                }
                editable={group.isCurrent}
                onSelect={() =>
                  group.isCurrent
                    ? onSelectSession(session.id)
                    : onSelectWorkspace(group.root, session.id)
                }
                onDelete={() => onDeleteSession(session.id)}
                onRename={onRenameSession ? (title) => onRenameSession(session.id, title) : undefined}
              />
            ))}
          </ul>
        </nav>
      ))}
    </div>
  );
}

function SessionRow({
  session,
  groupName,
  active,
  running,
  editable,
  onSelect,
  onDelete,
  onRename,
}: {
  session: SessionItem;
  groupName: string;
  active: boolean;
  running: boolean;
  editable: boolean;
  onSelect(): void;
  onDelete(): void;
  onRename?(title: string): void;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(session.title);

  const commit = () => {
    const title = draft.trim();
    setEditing(false);
    if (title && title !== session.title) onRename?.(title);
  };

  if (editing) {
    return (
      <li className={styles.row}>
        <input
          className={styles.renameInput}
          value={draft}
          autoFocus
          maxLength={80}
          aria-label="会话名称"
          onChange={(event) => setDraft(event.target.value)}
          onBlur={commit}
          onKeyDown={(event) => {
            if (event.key === "Enter") commit();
            if (event.key === "Escape") setEditing(false);
          }}
        />
      </li>
    );
  }

  return (
    <li className={`${styles.row}${active ? ` ${styles.rowActive}` : ""}`}>
      <button
        type="button"
        className={styles.item}
        onClick={onSelect}
        aria-current={active ? "page" : undefined}
        title={`${groupName} · ${session.title}`}
      >
        <span className={styles.itemTitle}>{session.title}</span>
        {running && <span className={styles.runningDot} aria-hidden />}
      </button>
      {editable && (
        <MenuButton
          label={`更多操作 ${session.title}`}
          className={`icon-btn icon-btn--sm ${styles.more}`}
          items={[
            ...(onRename
              ? [
                  {
                    label: "重命名",
                    icon: "pencil" as const,
                    onSelect: () => {
                      setDraft(session.title);
                      setEditing(true);
                    },
                  },
                ]
              : []),
            { label: "删除", icon: "trash" as const, tone: "danger" as const, onSelect: onDelete },
          ]}
        />
      )}
    </li>
  );
}
