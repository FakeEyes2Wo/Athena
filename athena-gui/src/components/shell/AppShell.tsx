import { ReactNode } from "react";

interface AppShellProps {
  sidebar: ReactNode;
  conversation: ReactNode;
  rightRail: ReactNode;
  contextSurface: ReactNode;
}

/** Top-level layout shell with four zones: sidebar, conversation, right rail, and context surface. */
export function AppShell({ sidebar, conversation, rightRail, contextSurface }: AppShellProps) {
  return (
    <div className="app-shell">
      <div className="app-shell__body">
        <aside className="app-shell__sidebar">{sidebar}</aside>
        <main className="app-shell__conversation">{conversation}</main>
        <aside className="app-shell__right-rail">{rightRail}</aside>
      </div>
      <section className="app-shell__context-surface">{contextSurface}</section>
    </div>
  );
}
