import { UnderlineNav } from "@primer/react";
import type { ModuleKey } from "../../types/ui";
import { Icon } from "../common/Icon";
import { MODULES } from "./navigation";
import styles from "./ModuleNav.module.css";

interface ModuleNavProps {
  module: ModuleKey;
  /** 各模块标签上的计数；缺省或为 0 时不显示。 */
  counts?: Partial<Record<ModuleKey, number>>;
  onSelect(module: ModuleKey): void;
}

/** 模块标签栏：GitHub 仓库页式 UnderlineNav，替代原左侧图标功能轨。 */
export function ModuleNav({ module, counts = {}, onSelect }: ModuleNavProps) {
  return (
    <div className={styles.nav}>
      <UnderlineNav aria-label="模块">
        {MODULES.map((item) => {
          const count = counts[item.key];
          return (
            <UnderlineNav.Item
              key={item.key}
              as="button"
              aria-current={module === item.key ? "page" : undefined}
              leadingVisual={<Icon name={item.icon} size={16} />}
              counter={count ? count : undefined}
              onSelect={(event) => {
                event.preventDefault();
                onSelect(item.key);
              }}
            >
              {item.label}
            </UnderlineNav.Item>
          );
        })}
      </UnderlineNav>
    </div>
  );
}
