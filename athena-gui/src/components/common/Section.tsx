import type { ReactNode } from "react";
import styles from "./Section.module.css";

/** 带边框的分组容器（对应 GitHub 的 Box），可选标题栏。 */
export function Section({ title, children }: { title?: ReactNode; children: ReactNode }) {
  return (
    <section className={styles.section}>
      {title && <h4 className={styles.title}>{title}</h4>}
      <div className={styles.body}>{children}</div>
    </section>
  );
}
