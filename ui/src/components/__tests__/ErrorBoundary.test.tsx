import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { ErrorBoundary } from '../ErrorBoundary';

function Boom(): never {
  throw new Error("Cannot read properties of undefined (reading 'slice')");
}

describe('ErrorBoundary — a viewer crash stays inside its panel', () => {
  it('renders the error message in place and keeps siblings mounted', () => {
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {});
    render(
      <div>
        <div>sibling panel</div>
        <ErrorBoundary label="trajectory viewer">
          <Boom />
        </ErrorBoundary>
      </div>,
    );
    expect(screen.getByRole('alert')).toHaveTextContent(
      /trajectory viewer hit a rendering error/,
    );
    expect(screen.getByText(/reading 'slice'/)).toBeInTheDocument();
    expect(screen.getByText('sibling panel')).toBeInTheDocument();
    spy.mockRestore();
  });

  it('renders children untouched when nothing throws', () => {
    render(
      <ErrorBoundary label="x">
        <span>fine</span>
      </ErrorBoundary>,
    );
    expect(screen.getByText('fine')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });
});
