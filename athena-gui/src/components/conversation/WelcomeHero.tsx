import { Icon } from "../common/Icon";
import styles from "./WelcomeHero.module.css";

const EXAMPLE_PROMPTS = [
  "用图像分类数据集，优化验证集准确率",
  "在文本数据上做回归，降低预测误差",
  "对现有模型做超参数搜索，提升 F1 分数",
];

interface WelcomeHeroProps {
  onPrompt(prompt: string): void;
}

/** Empty-conversation landing with brand, tagline, and clickable example prompts. */
export function WelcomeHero({ onPrompt }: WelcomeHeroProps) {
  return (
    <div className={styles["welcome-hero"]}>
      <div className={styles["welcome-hero__mark"]} aria-hidden>
        <Icon name="sparkles" size={26} />
      </div>
      <h2 className={styles["welcome-hero__title"]}>Athena</h2>
      <p className={styles["welcome-hero__subtitle"]}>对话式 ML 研究编排 · 从假设到实验全自动</p>
      <div className={styles["welcome-hero__prompts"]}>
        {EXAMPLE_PROMPTS.map((prompt) => (
          <button
            key={prompt}
            className={styles["welcome-hero__prompt"]}
            onClick={() => onPrompt(prompt)}
          >
            {prompt}
          </button>
        ))}
      </div>
    </div>
  );
}
