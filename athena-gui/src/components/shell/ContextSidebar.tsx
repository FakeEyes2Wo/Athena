import { useMemo } from "react";
import { ActionList, Button, NavList } from "@primer/react";
import { FileDirectoryIcon, PlusIcon, TrashIcon } from "@primer/octicons-react";
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
    <aside className={styles.sidebar} aria-label="上下文侧栏">
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
        <NavList aria-label="详情视图" className={styles.related}>
          <NavList.Group title="详情视图">
            {related.map((item) => (
              <NavList.Item key={item.key} as="button" onClick={() => onOpenDrawer(item.key)}>
                <NavList.LeadingVisual>
                  <Icon name={item.icon} size={16} />
                </NavList.LeadingVisual>
                {item.label}
              </NavList.Item>
            ))}
          </NavList.Group>
        </NavList>
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
  const otherGroups = useMemo(
    () => recentRoots
      .filter((root) => root !== currentRoot)
      .map((root): WorkspaceGroup => ({
        root,
        name: basename(root),
        isCurrent: false,
        sessions: loadWorkspaceSessions(root),
      })),
    [currentRoot, recentRoots],
  );

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
      <div className={styles.newSession}>
        <Button block leadingVisual={PlusIcon} onClick={onNewSession}>
          新会话
        </Button>
      </div>
      <div className={styles.workspaceScroll} data-testid="workspace-scroll">
        {groups.map((group) => (
          <section key={group.root} className={styles.workspaceGroup}>
            <Button
              variant="invisible"
              size="small"
              alignContent="start"
              block
              leadingVisual={FileDirectoryIcon}
              className={styles.workspaceHeader}
              onClick={() => {
                if (!group.isCurrent) onSelectWorkspace(group.root);
              }}
              disabled={group.isCurrent}
              title={group.isCurrent ? "当前工作区" : `切换到 ${group.root}`}
            >
              {group.name}
            </Button>
            {group.sessions.length === 0 ? (
              <p className={styles.hint}>暂无会话</p>
            ) : (
              <ActionList aria-label={`${group.name} 会话`}>
                {group.sessions.map((session) => (
                  <ActionList.Item
                    key={session.id}
                    active={group.isCurrent && session.id === currentSessionId}
                    title={`${group.name} · ${session.title}`}
                    onSelect={() =>
                      group.isCurrent ? onSelectSession(session.id) : onSelectWorkspace(group.root, session.id)
                    }
                  >
                    {session.title}
                    {group.isCurrent && (
                      <ActionList.TrailingAction
                        label={`删除会话 ${session.title}`}
                        icon={TrashIcon}
                        onClick={() => onDeleteSession(session.id)}
                      />
                    )}
                  </ActionList.Item>
                ))}
              </ActionList>
            )}
          </section>
        ))}
      </div>
      <footer className={styles.sidebarFooter}>
        <Button block variant="invisible" onClick={onSwitchWorkspace}>
          切换工作区
        </Button>
      </footer>
    </div>
  );
}
