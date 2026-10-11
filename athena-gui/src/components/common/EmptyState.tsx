import type { ReactNode } from "react";
import { Icon, type IconName } from "./Icon";

interface EmptyStateProps {
  icon: IconName;
  /** Short heading. */
  title?: string;
  /** Guidance text (kept as ``message`` for older call sites). */
  message?: ReactNode;
  action?: ReactNode;
}

/** Shared empty-state placeholder: icon, optional title, guidance and action. */
export function EmptyState({ icon, title, message, action }: EmptyStateProps) {
  const spinning = icon === "loader";
  return (
    <div className="empty-state" role={spinning ? "status" : undefined}>
      <div className="empty-state__icon">
        <Icon name={icon} size={20} className={spinning ? "icon--spin" : undefined} />
      </div>
      {title && <p className="empty-state__title">{title}</p>}
      {message && <p className="empty-state__desc">{message}</p>}
      {action && <div className="empty-state__action">{action}</div>}
    </div>
  );
}
