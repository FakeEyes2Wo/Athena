import { Component, type ReactNode } from "react";
import { Icon } from "./Icon";

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
        <div className="callout callout--error" role="alert" style={{ margin: "var(--space-lg)" }}>
          <Icon name="alert" size={16} />
          <div className="callout__body">
            <h3 className="callout__title" style={{ fontSize: 13 }}>面板加载失败</h3>
            <p style={{ margin: 0 }}>{this.state.error.message}</p>
            <button
              type="button"
              className="btn btn--sm"
              style={{ marginTop: 8 }}
              onClick={() => this.setState({ error: null })}
            >
              重试
            </button>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}
