import { Icon, type IconName } from "./Icon";

interface EmptyStateProps {
  icon: IconName;
  message: string;
}

/** Shared empty-state placeholder with an icon and guidance text. */
export function EmptyState({ icon, message }: EmptyStateProps) {
  return (
    <div className="empty-state">
      <div className="empty-state__icon">
        <Icon name={icon} size={22} />
      </div>
      {message}
    </div>
  );
}
