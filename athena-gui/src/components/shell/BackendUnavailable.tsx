import { useState } from "react";
import { BrandMark } from "../common/BrandMark";
import { Icon } from "../common/Icon";
import styles from "./BackendUnavailable.module.css";

interface BackendUnavailableProps {
  error: string | null;
  onRetry(): Promise<void> | void;
}

/** Startup screen when the Python gateway cannot be reached (browser mode). */
export function BackendUnavailable({ error, onRetry }: BackendUnavailableProps) {
  const [retrying, setRetrying] = useState(false);

  return (
    <div className={styles.screen}>
      <div className={styles.card}>
        <BrandMark size={30} withName />
        <div className={styles.icon}>
          <Icon name="disconnected" size={22} />
        </div>
        <h1 className={styles.title}>无法连接到 Athena 后端</h1>
        <p className={styles.text}>
          界面需要本机的 Python 网关提供服务。请在仓库的 <code>athena-gui</code> 目录中运行下面的命令，它会同时启动网关与界面：
        </p>
        <pre className={styles.command}>npm run dev:web</pre>
        <p className={styles.text}>后端启动后此页面会自动重连，也可以手动重试。</p>
        {error && <p className={styles.detail}>{error}</p>}
        <button
          type="button"
          className="btn btn--primary"
          disabled={retrying}
          onClick={async () => {
            setRetrying(true);
            try {
              await onRetry();
            } finally {
              setRetrying(false);
            }
          }}
        >
          {retrying ? <Icon name="loader" size={14} className="icon--spin" /> : <Icon name="refresh" size={14} />}
          重新连接
        </button>
      </div>
    </div>
  );
}
