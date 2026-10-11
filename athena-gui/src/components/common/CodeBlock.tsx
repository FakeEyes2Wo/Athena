import type { ReactNode } from "react";
import styles from "./CodeBlock.module.css";

/** 等宽代码块：GitHub 代码底色与 Primer 等宽字体栈。 */
export function CodeBlock({ children, className }: { children: ReactNode; className?: string }) {
  return <pre className={className ? `${styles.code} ${className}` : styles.code}>{children}</pre>;
}
