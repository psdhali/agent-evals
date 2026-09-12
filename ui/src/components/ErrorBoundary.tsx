import { Component, type ErrorInfo, type ReactNode } from 'react';

// A render error anywhere below the router used to unmount the WHOLE app —
// the operator saw a blank page with no way back (2026-09-07: one malformed
// tool call in a trajectory did exactly that). React only recovers from a
// render error at a boundary, so each artifact viewer gets one: the failing
// panel shows the error and the rest of the page keeps working.

interface Props {
  /** what the failing region is, for the message ("trajectory viewer") */
  label: string;
  children: ReactNode;
}

interface State {
  error: Error | null;
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // Keep the stack in the console for the next developer; the UI shows
    // the message only.
    console.error(
      `[${this.props.label}] render failed`,
      error,
      info.componentStack,
    );
  }

  render(): ReactNode {
    const { error } = this.state;
    if (error) {
      return (
        <div
          role="alert"
          className="m-3 rounded-md border border-rose-200 bg-rose-50/60 p-3 text-xs text-rose-700 dark:border-rose-900 dark:bg-rose-950/30 dark:text-rose-300"
        >
          <div className="font-semibold">
            The {this.props.label} hit a rendering error and was stopped.
          </div>
          <pre className="mt-1 whitespace-pre-wrap font-mono text-[11px]">
            {error.message}
          </pre>
          <div className="mt-1 text-[11px] text-rose-600/80 dark:text-rose-400/80">
            The rest of the page is unaffected. Switch tab or reload to retry;
            the raw artifact is still downloadable from the API.
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}
