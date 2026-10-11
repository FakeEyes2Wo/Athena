import { useEffect, useId, useRef, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { Icon } from "./Icon";
import styles from "./Dialog.module.css";

const FOCUSABLE =
  "button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex='-1'])";

interface DialogProps {
  open: boolean;
  title: ReactNode;
  description?: ReactNode;
  onClose(): void;
  children?: ReactNode;
  footer?: ReactNode;
  size?: "sm" | "md" | "lg";
  /** Disallow Escape / backdrop dismissal (e.g. while saving). */
  locked?: boolean;
  /** Extra class on the panel (layout overrides). */
  className?: string;
  /** Extra class on the scrollable body. */
  bodyClassName?: string;
  /** Hide the header close button. */
  hideClose?: boolean;
}

/** Accessible modal dialog: portal, backdrop, Escape, focus trap and focus restore. */
export function Dialog({
  open,
  title,
  description,
  onClose,
  children,
  footer,
  size = "md",
  locked = false,
  className,
  bodyClassName,
  hideClose = false,
}: DialogProps) {
  const panelRef = useRef<HTMLDivElement | null>(null);
  const titleId = useId();
  const descId = useId();
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  const lockedRef = useRef(locked);
  lockedRef.current = locked;

  useEffect(() => {
    if (!open) return;
    const previous = document.activeElement as HTMLElement | null;
    const panel = panelRef.current;
    const first =
      panel?.querySelector<HTMLElement>("[data-autofocus]") ??
      panel?.querySelector<HTMLElement>(FOCUSABLE) ??
      panel;
    first?.focus();

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        if (!lockedRef.current) {
          event.stopPropagation();
          onCloseRef.current();
        }
        return;
      }
      if (event.key !== "Tab" || !panelRef.current) return;
      const items = Array.from(panelRef.current.querySelectorAll<HTMLElement>(FOCUSABLE));
      if (items.length === 0) return;
      const head = items[0];
      const tail = items[items.length - 1];
      if (event.shiftKey && document.activeElement === head) {
        event.preventDefault();
        tail.focus();
      } else if (!event.shiftKey && document.activeElement === tail) {
        event.preventDefault();
        head.focus();
      }
    };
    document.addEventListener("keydown", onKeyDown, true);
    return () => {
      document.removeEventListener("keydown", onKeyDown, true);
      if (previous && document.contains(previous)) previous.focus();
    };
  }, [open]);

  if (!open) return null;

  return createPortal(
    <div
      className={styles.backdrop}
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !locked) onClose();
      }}
    >
      <div
        ref={panelRef}
        className={`${styles.panel} ${styles[`panel--${size}`]}${className ? ` ${className}` : ""}`}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={description ? descId : undefined}
        tabIndex={-1}
      >
        <header className={styles.header}>
          <div className={styles.heading}>
            <h2 id={titleId} className={styles.title}>{title}</h2>
            {description && <p id={descId} className={styles.description}>{description}</p>}
          </div>
          {!hideClose && (
            <button
              type="button"
              className="icon-btn"
              onClick={onClose}
              disabled={locked}
              aria-label="关闭"
            >
              <Icon name="close" size={17} />
            </button>
          )}
        </header>
        {children !== undefined && (
          <div className={bodyClassName ? `${styles.body} ${bodyClassName}` : styles.body}>
            {children}
          </div>
        )}
        {footer && <footer className={styles.footer}>{footer}</footer>}
      </div>
    </div>,
    document.body,
  );
}
