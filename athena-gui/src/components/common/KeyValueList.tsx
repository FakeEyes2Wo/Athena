import type { ReactNode } from "react";
import styles from "./KeyValueList.module.css";

export interface KeyValueItem {
  label: string;
  value: ReactNode;
  /** 用等宽字体显示（路径、提交号、ID）。 */
  mono?: boolean;
}

/** 键值列表；空值显示为「—」。 */
export function KeyValueList({ items, columns = 1 }: { items: KeyValueItem[]; columns?: 1 | 2 }) {
  return (
    <dl className={columns === 2 ? `${styles.list} ${styles.twoColumns}` : styles.list}>
      {items.map((item) => (
        <div key={item.label} className={styles.row}>
          <dt className={styles.label}>{item.label}</dt>
          <dd className={item.mono ? `${styles.value} ${styles.mono}` : styles.value}>
            {item.value === "" || item.value == null ? "—" : item.value}
          </dd>
        </div>
      ))}
    </dl>
  );
}
