import { Component, type ReactNode } from "react";
import { Flash } from "@primer/react";
import styles from "./ErrorBoundary.module.css";

interface ErrorBoundaryProps {
  children: ReactNode;
}

interface ErrorBoundaryState {
  error: Error | null;
}

/** Catches render errors from lazy-loaded panels so one failure never blanks the app. */
export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  state: ErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { error };
  }

  render() {
    if (this.state.error) {
      return (
        <Flash variant="danger" className={styles.flash}>
          <h3 className={styles.title}>面板加载失败</h3>
          <p className={styles.message}>{this.state.error.message}</p>
        </Flash>
      );
    }
    return this.props.children;
  }
}
