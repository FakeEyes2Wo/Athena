import { useEffect, useLayoutEffect, useRef, useState, type CSSProperties } from "react";
import { createPortal } from "react-dom";
import { Icon, type IconName } from "./Icon";
import styles from "./Menu.module.css";

export interface MenuItem {
  label: string;
  icon?: IconName;
  tone?: "danger";
  disabled?: boolean;
  onSelect(): void;
}

interface MenuButtonProps {
  /** Accessible name of the trigger. */
  label: string;
  items: MenuItem[];
  icon?: IconName;
  className?: string;
  align?: "start" | "end";
}

/** Icon trigger + floating menu (portal, outside-click/Escape close, arrow keys). */
export function MenuButton({ label, items, icon = "more", className, align = "end" }: MenuButtonProps) {
  const [open, setOpen] = useState(false);
  const [position, setPosition] = useState<CSSProperties>({});
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const menuRef = useRef<HTMLDivElement | null>(null);

  useLayoutEffect(() => {
    if (!open || !triggerRef.current) return;
    const rect = triggerRef.current.getBoundingClientRect();
    const below = window.innerHeight - rect.bottom > 160;
    setPosition({
      top: below ? rect.bottom + 4 : undefined,
      bottom: below ? undefined : window.innerHeight - rect.top + 4,
      left: align === "start" ? rect.left : undefined,
      right: align === "end" ? Math.max(8, window.innerWidth - rect.right) : undefined,
    });
  }, [align, open]);

  useEffect(() => {
    if (!open) return;
    menuRef.current?.querySelector<HTMLButtonElement>("button:not([disabled])")?.focus();
    const close = () => setOpen(false);
    const onPointer = (event: MouseEvent) => {
      const target = event.target as Node;
      if (menuRef.current?.contains(target) || triggerRef.current?.contains(target)) return;
      close();
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.stopPropagation();
        close();
        triggerRef.current?.focus();
        return;
      }
      if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
      const buttons = Array.from(
        menuRef.current?.querySelectorAll<HTMLButtonElement>("button:not([disabled])") ?? [],
      );
      if (buttons.length === 0) return;
      event.preventDefault();
      const index = buttons.indexOf(document.activeElement as HTMLButtonElement);
      const delta = event.key === "ArrowDown" ? 1 : -1;
      buttons[(index + delta + buttons.length) % buttons.length].focus();
    };
    document.addEventListener("mousedown", onPointer);
    document.addEventListener("keydown", onKey, true);
    window.addEventListener("resize", close);
    window.addEventListener("blur", close);
    return () => {
      document.removeEventListener("mousedown", onPointer);
      document.removeEventListener("keydown", onKey, true);
      window.removeEventListener("resize", close);
      window.removeEventListener("blur", close);
    };
  }, [open]);

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        className={className ?? "icon-btn icon-btn--sm"}
        aria-label={label}
        title={label}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={(event) => {
          event.stopPropagation();
          setOpen((value) => !value);
        }}
      >
        <Icon name={icon} size={15} />
      </button>
      {open &&
        createPortal(
          <div ref={menuRef} className={styles.menu} role="menu" aria-label={label} style={position}>
            {items.map((item) => (
              <button
                key={item.label}
                type="button"
                role="menuitem"
                className={`${styles.item}${item.tone === "danger" ? ` ${styles.danger}` : ""}`}
                disabled={item.disabled}
                onClick={() => {
                  setOpen(false);
                  item.onSelect();
                }}
              >
                {item.icon && <Icon name={item.icon} size={14} />}
                {item.label}
              </button>
            ))}
          </div>,
          document.body,
        )}
    </>
  );
}
