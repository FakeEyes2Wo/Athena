import { Suspense } from "react";
import type { ContextPanelKey } from "../../types/ui";
import { Icon } from "../common/Icon";
import { EmptyState } from "../common/EmptyState";
import { ErrorBoundary } from "../common/ErrorBoundary";
import { PANELS, PANEL_TITLES } from "../context/panels";
import styles from "./ContextDrawer.module.css";

interface ContextDrawerProps {
  panel: ContextPanelKey;
  onClose(): void;
}

/** 按需打开的右侧详情抽屉：承载指标/假设图/差异等次要视图。 */
export function ContextDrawer({ panel, onClose }: ContextDrawerProps) {
  const Panel = PANELS[panel];
  const title = PANEL_TITLES[panel] ?? panel;

  return (
    <aside className={styles.drawer} role="complementary" aria-label={title}>
      <header className={styles.header}>
        <h2>{title}</h2>
        <button type="button" className={styles.close} onClick={onClose} aria-label="关闭详情">
          <Icon name="close" size={16} />
        </button>
      </header>
      <div className={styles.body}>
        <Suspense fallback={<EmptyState icon="loader" message="加载中…" />}>
          <ErrorBoundary>
            {Panel ? <Panel /> : <EmptyState icon="loader" message="该视图暂不可用" />}
          </ErrorBoundary>
        </Suspense>
      </div>
    </aside>
  );
}
