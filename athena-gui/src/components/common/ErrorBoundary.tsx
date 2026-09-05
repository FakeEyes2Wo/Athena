import { Component, type ReactNode } from "react";

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
        <div className="card card--error" style={{ margin: "var(--space-lg)" }}>
          <h3 className="card__title">面板加载失败</h3>
          <p className="card__body">{this.state.error.message}</p>
        </div>
      );
    }
    return this.props.children;
  }
}
