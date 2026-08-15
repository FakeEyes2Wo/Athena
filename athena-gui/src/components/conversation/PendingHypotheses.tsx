import styles from "./PendingHypotheses.module.css";

interface PendingHypothesesProps {
  pending: Array<{ id: string; statement: string }>;
  onSelect(id: string): void;
}

/** Manual-mode hypothesis selector shown when PROPOSED hypotheses await selection. */
export function PendingHypotheses({ pending, onSelect }: PendingHypothesesProps) {
  if (pending.length === 0) return null;

  return (
    <div className={styles["pending-hypotheses"]}>
      <div className={styles["pending-hypotheses__title"]}>待选假设 ({pending.length})</div>
      <ul className={styles["pending-hypotheses__list"]}>
        {pending.map((h) => (
          <li key={h.id} className={styles["pending-hypotheses__item"]}>
            <span className={styles["pending-hypotheses__statement"]}>{h.statement}</span>
            <button className="btn btn--subtle btn--sm" onClick={() => onSelect(h.id)}>
              选择
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
