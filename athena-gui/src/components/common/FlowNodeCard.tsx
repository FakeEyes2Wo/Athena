import type { ReactNode } from "react";
import { Label } from "@primer/react";
import { Handle, Position } from "reactflow";
import { hypothesisBorderColor, hypothesisVariant } from "./hypothesisStatus";
import styles from "./FlowNodeCard.module.css";

interface FlowNodeCardProps {
  statement: string;
  primaryText: string;
  /** 假设状态：决定左边框与标签配色。 */
  status: string;
  /** 标签文字，如实验状态或「SUPPORTED ★ SOTA」。 */
  badge: string;
}

/** 研究树 / 假设图共用的 ReactFlow 节点：GitHub 卡片 + 状态标签 + 状态色左边框。 */
export function FlowNodeCard({ statement, primaryText, status, badge }: FlowNodeCardProps) {
  return (
    <div className={styles.node} style={{ borderLeftColor: hypothesisBorderColor(status) }}>
      <Handle type="target" position={Position.Top} />
      <div className={styles.title}>
        {statement.slice(0, 48)}
        {statement.length > 48 ? "…" : ""}
      </div>
      <div className={styles.meta}>
        Primary: <strong>{primaryText}</strong>
      </div>
      <Label variant={hypothesisVariant(status)}>{badge}</Label>
      <Handle type="source" position={Position.Bottom} />
    </div>
  );
}

/** 小地图节点的 class：按假设状态着色（SVG 的 fill 属性不解析 CSS 变量，改由 CSS 设置）。 */
export function minimapNodeClass(status: string): string {
  return `${styles.minimapNode} ${styles[`minimap-${hypothesisVariant(status)}`] ?? ""}`;
}

/** 图容器：Primer 边框与圆角，并把 ReactFlow 的控件、小地图、背景点阵改用 Primer 变量。 */
export function GraphFrame({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className={styles.frame} role="region" aria-label={label}>
      {children}
    </div>
  );
}
