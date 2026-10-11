import type { ReactNode } from "react";
import styles from "./MainHeader.module.css";

interface MainHeaderProps {
  title?: ReactNode;
  subtitle?: ReactNode;
  /** Content after the title (status chips, tabs, ...). */
  children?: ReactNode;
  /** Right-aligned actions. */
  actions?: ReactNode;
}

/** Slim header shared by the conversation and every research page. */
export function MainHeader({ title, subtitle, children, actions }: MainHeaderProps) {
  return (
    <header className={styles.header}>
      {(title || subtitle) && (
        <div className={styles.titleBlock}>
          {title && <h1 className={styles.title}>{title}</h1>}
          {subtitle && <p className={styles.subtitle}>{subtitle}</p>}
        </div>
      )}
      {children && <div className={styles.middle}>{children}</div>}
      {actions && <div className={styles.actions}>{actions}</div>}
    </header>
  );
}
