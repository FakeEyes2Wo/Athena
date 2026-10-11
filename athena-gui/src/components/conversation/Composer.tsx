import {
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
  type ReactNode,
} from "react";
import { Icon } from "../common/Icon";
import styles from "./Composer.module.css";

/** Slash commands understood by ``usePipeline.sendPrompt`` (mirrors the TUI). */
export const SLASH_COMMANDS: Array<{ command: string; args?: string; description: string }> = [
  { command: "/pause", description: "暂停当前研究" },
  { command: "/resume", description: "继续已暂停或中断的研究" },
  { command: "/stop", description: "停止当前研究" },
  { command: "/manual", description: "切换为手动选择假设" },
  { command: "/auto", description: "切换为自动选择假设" },
  { command: "/select", args: "<假设 ID>", description: "手动模式下选择要验证的假设" },
  { command: "/help", description: "显示可用命令" },
];

interface ComposerProps {
  value: string;
  onChange(value: string): void;
  onSubmit(text: string): Promise<void> | void;
  placeholder: string;
  /** Disable typing entirely (e.g. while Athena is working on a step). */
  disabled?: boolean;
  /** Offer the slash-command menu. */
  commands?: boolean;
  /** Small context row inside the box, left of the send button. */
  toolbar?: ReactNode;
  /** Larger variant used on the welcome screen. */
  size?: "md" | "lg";
  autoFocus?: boolean;
  /** Bump to move focus into the box with the caret at the end (e.g. after an example fills it). */
  focusSignal?: number;
  /** Accessible name of the text box. */
  label?: string;
}

const MAX_HEIGHT = 240;

/** Rounded message box: auto-growing textarea, Enter to send, slash-command menu. */
export function Composer({
  value,
  onChange,
  onSubmit,
  placeholder,
  disabled = false,
  commands = false,
  toolbar,
  size = "md",
  autoFocus = false,
  focusSignal = 0,
  label = "消息",
}: ComposerProps) {
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const [sending, setSending] = useState(false);
  const [menuIndex, setMenuIndex] = useState(0);
  const [menuDismissed, setMenuDismissed] = useState(false);

  const matches = useMemo(() => {
    if (!commands || !value.startsWith("/") || /\s/.test(value)) return [];
    const prefix = value.toLowerCase();
    return SLASH_COMMANDS.filter((item) => item.command.startsWith(prefix));
  }, [commands, value]);
  const menuOpen = matches.length > 0 && !menuDismissed && !(matches.length === 1 && matches[0].command === value);

  useEffect(() => {
    setMenuIndex(0);
    if (!value.startsWith("/")) setMenuDismissed(false);
  }, [value]);

  // Grow with content up to a cap, then scroll.
  useLayoutEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, MAX_HEIGHT)}px`;
  }, [value]);

  useEffect(() => {
    if (autoFocus && !disabled) textareaRef.current?.focus();
  }, [autoFocus, disabled]);

  useEffect(() => {
    const el = textareaRef.current;
    if (!focusSignal || !el || disabled) return;
    el.focus();
    el.setSelectionRange(el.value.length, el.value.length);
  }, [focusSignal, disabled]);

  const canSend = !disabled && !sending && value.trim().length > 0;

  async function submit() {
    if (!canSend) return;
    const text = value;
    onChange("");
    setSending(true);
    try {
      await onSubmit(text);
    } catch {
      // Keep what the user typed when sending fails (the error is shown in the chat).
      onChange(text);
    } finally {
      setSending(false);
      textareaRef.current?.focus();
    }
  }

  function pick(index: number) {
    const item = matches[index];
    if (!item) return;
    onChange(item.args ? `${item.command} ` : item.command);
    setMenuDismissed(true);
    textareaRef.current?.focus();
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    // Never treat Enter as "send" while an IME (Chinese/Japanese input) is composing.
    if (event.nativeEvent.isComposing || event.keyCode === 229) return;
    if (menuOpen) {
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        const delta = event.key === "ArrowDown" ? 1 : -1;
        setMenuIndex((index) => (index + delta + matches.length) % matches.length);
        return;
      }
      if (event.key === "Tab" || (event.key === "Enter" && !event.shiftKey)) {
        event.preventDefault();
        pick(menuIndex);
        return;
      }
      if (event.key === "Escape") {
        event.preventDefault();
        setMenuDismissed(true);
        return;
      }
    }
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      void submit();
    }
  }

  return (
    <form
      className={`${styles.composer} ${size === "lg" ? styles.large : ""}`}
      data-disabled={disabled || undefined}
      onSubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
      onMouseDown={(event) => {
        // Clicking the padding focuses the text box, like a single input.
        if (event.target === event.currentTarget) {
          event.preventDefault();
          textareaRef.current?.focus();
        }
      }}
    >
      {menuOpen && (
        <ul className={styles.menu} role="listbox" aria-label="命令">
          {matches.map((item, index) => (
            <li key={item.command}>
              <button
                type="button"
                role="option"
                aria-selected={index === menuIndex}
                className={styles.menuItem}
                onMouseDown={(event) => event.preventDefault()}
                onMouseEnter={() => setMenuIndex(index)}
                onClick={() => pick(index)}
              >
                <span className={styles.menuCommand}>
                  {item.command}
                  {item.args && <span className={styles.menuArgs}> {item.args}</span>}
                </span>
                <span className={styles.menuDesc}>{item.description}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
      <textarea
        ref={textareaRef}
        className={styles.input}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        onKeyDown={onKeyDown}
        placeholder={placeholder}
        aria-label={label}
        rows={1}
        disabled={disabled}
      />
      <div className={styles.bar}>
        <div className={styles.toolbar}>{toolbar}</div>
        <button
          className={styles.send}
          type="submit"
          disabled={!canSend}
          aria-label="发送"
          title="发送（Enter）"
        >
          {sending ? (
            <Icon name="loader" size={16} className="icon--spin" />
          ) : (
            <Icon name="send" size={17} strokeWidth={2.3} />
          )}
        </button>
      </div>
    </form>
  );
}
