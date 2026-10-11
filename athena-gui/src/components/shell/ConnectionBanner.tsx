import { wsBackend, type BackendStatus } from "../../lib/ws-backend";
import { Icon } from "../common/Icon";
import styles from "./ConnectionBanner.module.css";

/** Top-of-content warning shown while the gateway WebSocket is down (browser mode). */
export function ConnectionBanner({ status }: { status: BackendStatus }) {
  if (status !== "closed") return null;
  return (
    <div className={styles.banner} role="status">
      <Icon name="disconnected" size={15} />
      <span className={styles.text}>
        与 Athena 后端的连接已断开，正在自动重连… 请确认后端进程（<code>npm run dev:web</code>）仍在运行。
      </span>
      <button
        type="button"
        className="btn btn--sm"
        onClick={() => {
          wsBackend.connect().catch(() => undefined);
        }}
      >
        立即重试
      </button>
    </div>
  );
}
