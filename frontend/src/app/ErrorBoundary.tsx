import { Component, type ErrorInfo, type ReactNode } from "react";

interface State {
  failed: boolean;
}

export class ErrorBoundary extends Component<{ children: ReactNode }, State> {
  override state: State = { failed: false };

  static getDerivedStateFromError(): State {
    return { failed: true };
  }

  override componentDidCatch(error: Error, info: ErrorInfo): void {
    // Component stack only; no application data is logged.
    console.error("UI error:", error.name, info.componentStack);
  }

  override render() {
    if (!this.state.failed) return this.props.children;
    return (
      <div role="alert" className="mx-auto mt-24 max-w-md rounded-lg border border-red-200 bg-red-50 p-6 text-sm text-red-900">
        <p className="text-base font-semibold">The interface hit an unexpected error.</p>
        <p className="mt-1">Reload the page. If it keeps happening, check the browser console and the backend logs.</p>
        <button
          type="button"
          className="mt-4 rounded-md bg-red-700 px-3.5 py-2 text-sm font-medium text-white hover:bg-red-800"
          onClick={() => window.location.reload()}
        >
          Reload
        </button>
      </div>
    );
  }
}
