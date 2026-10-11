import type { ReactNode } from "react";
import { PageHeader } from "@primer/react";
import styles from "./Panel.module.css";

interface PanelProps {
  title: ReactNode;
  actions?: ReactNode;
  /** 撑满父容器高度（图类面板需要确定高度）。 */
  fill?: boolean;
  children?: ReactNode;
}

/** 数据面板：PageHeader 标题区 + 右侧操作 + 内容区。 */
export function Panel({ title, actions, fill = false, children }: PanelProps) {
  return (
    <div className={fill ? `${styles.panel} ${styles.fill}` : styles.panel}>
      <PageHeader className={styles.header}>
        <PageHeader.TitleArea variant="medium">
          <PageHeader.Title as="h3">{title}</PageHeader.Title>
        </PageHeader.TitleArea>
        {actions && <PageHeader.Actions>{actions}</PageHeader.Actions>}
      </PageHeader>
      {children}
    </div>
  );
}
