import { Button, CounterLabel, Label } from "@primer/react";
import { hypothesisVariant } from "../common/hypothesisStatus";
import styles from "./PendingHypotheses.module.css";

interface PendingHypothesesProps {
  pending: Array<{ id: string; statement: string }>;
  onSelect(id: string): void;
}

/** Manual-mode hypothesis selector shown when PROPOSED hypotheses await selection. */
export function PendingHypotheses({ pending, onSelect }: PendingHypothesesProps) {
  if (pending.length === 0) return null;

  return (
    <section className={styles.panel} aria-label="待选假设">
      <div className={styles.title}>
        待选假设 <CounterLabel>{pending.length}</CounterLabel>
      </div>
      <ul className={styles.list}>
        {pending.map((h) => (
          <li key={h.id} className={styles.item}>
            <Label variant={hypothesisVariant("PROPOSED")}>PROPOSED</Label>
            <span className={styles.statement}>{h.statement}</span>
            <Button size="small" onClick={() => onSelect(h.id)}>
              选择
            </Button>
          </li>
        ))}
      </ul>
    </section>
  );
}
