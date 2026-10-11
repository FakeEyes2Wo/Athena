import { useState } from "react";
import { Icon } from "../common/Icon";
import styles from "./PendingHypotheses.module.css";

interface PendingHypothesesProps {
  pending: Array<{ id: string; statement: string }>;
  onSelect(id: string): void;
}

/** Manual-mode hypothesis selector shown when PROPOSED hypotheses await selection. */
export function PendingHypotheses({ pending, onSelect }: PendingHypothesesProps) {
  const [open, setOpen] = useState(true);
  if (pending.length === 0) return null;

  return (
    <section className={styles.strip} aria-label="待选假设">
      <button type="button" className={styles.header} onClick={() => setOpen((v) => !v)} aria-expanded={open}>
        <Icon name="lightbulb" size={15} />
        <span className={styles.title}>手动模式 · {pending.length} 个待选假设</span>
        <span className={styles.hint}>选择下一个要验证的假设</span>
        <Icon name={open ? "chevronUp" : "chevronDown"} size={15} />
      </button>
      {open && (
        <ul className={styles.list}>
          {pending.map((h) => (
            <li key={h.id} className={styles.item}>
              <span className={styles.id}>{h.id}</span>
              <span className={styles.statement}>{h.statement}</span>
              <button type="button" className="btn btn--subtle btn--sm" onClick={() => onSelect(h.id)}>
                选择
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
