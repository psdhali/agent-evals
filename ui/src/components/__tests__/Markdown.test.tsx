import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { Markdown } from '../Markdown';

// 2026-09-08: the judge's pass report is Markdown from our own prompt — this
// renderer covers exactly that subset, and never interprets HTML.
describe('Markdown', () => {
  it('renders headings, paragraphs, lists, bold, italic and inline code', () => {
    render(
      <Markdown
        source={[
          '## Overview',
          '500 attempts judged. **Two** showed _contamination_ (`django__django-10973`).',
          '',
          '### Findings per dimension',
          '- **environment_problem** — 397 attempts: `rg: command not found`',
          '- loop — 118 attempts',
          '  continued on the next line',
          '',
          '1. first',
          '2. second',
        ].join('\n')}
      />,
    );
    expect(
      screen.getByRole('heading', { name: 'Overview' }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole('heading', { name: 'Findings per dimension' }),
    ).toBeInTheDocument();
    expect(screen.getByText('Two').tagName).toBe('STRONG');
    expect(screen.getByText('contamination').tagName).toBe('EM');
    expect(screen.getByText('django__django-10973').tagName).toBe('CODE');
    const items = screen.getAllByRole('listitem').map((li) => li.textContent);
    expect(items).toEqual([
      'environment_problem — 397 attempts: rg: command not found',
      'loop — 118 attempts continued on the next line',
      'first',
      'second',
    ]);
    // markdown symbols never leak through as text
    expect(screen.queryByText(/##/)).not.toBeInTheDocument();
    expect(screen.queryByText(/\*\*/)).not.toBeInTheDocument();
  });

  it('renders a fenced code block verbatim and never interprets HTML', () => {
    render(
      <Markdown
        source={
          '```\nrg --version\n```\n\n<script>alert(1)</script> stays text'
        }
      />,
    );
    expect(screen.getByText('rg --version').tagName).toBe('PRE');
    expect(
      screen.getByText('<script>alert(1)</script> stays text'),
    ).toBeInTheDocument();
    expect(document.querySelector('script')).toBeNull();
  });

  it('never reads intra-word underscores as emphasis (instance ids, snake_case)', () => {
    // 2026-09-08: the codex report's "django__django-11239/1, django__django-11728/1"
    // rendered as alternating italics — `_…_` matched across the ids.
    render(
      <Markdown
        source={
          'affects django__django-11239/1, django__django-11728/1 and tool_efficiency scores; _real emphasis_ still works'
        }
      />,
    );
    expect(document.querySelectorAll('em')).toHaveLength(1);
    expect(screen.getByText('real emphasis').tagName).toBe('EM');
    expect(
      screen.getByText(
        /affects django__django-11239\/1, django__django-11728\/1 and tool_efficiency scores;/,
      ),
    ).toBeInTheDocument();
  });

  it('renders an empty source as an empty container', () => {
    render(<Markdown source="" />);
    expect(screen.getByTestId('markdown')).toBeEmptyDOMElement();
  });
});
