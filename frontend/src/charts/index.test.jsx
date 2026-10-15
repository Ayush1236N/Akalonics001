import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import ChartView, { ALL_CHART_TYPES } from './index.jsx';

describe('chart dispatcher', () => {
  it('covers all 37 chart types exactly once', () => {
    expect(ALL_CHART_TYPES).toHaveLength(37);
    expect(new Set(ALL_CHART_TYPES).size).toBe(37);
  });

  it('renders an empty state when the slide has no data', () => {
    render(<ChartView slide={{ type: 'bar', title: 'x' }} />);
    expect(screen.getByText(/No data for this chart/i)).toBeInTheDocument();
  });

  it('falls back to the accessible data table for every chart type', () => {
    for (const type of ALL_CHART_TYPES) {
      const slide = {
        id: `${type}-0`,
        type,
        title: `${type} slide`,
        spec: { type },
        data: { labels: ['North', 'South'], datasets: [{ label: 'revenue', data: [10, 20] }] },
      };
      const { unmount } = render(<ChartView slide={slide} showTable />);
      expect(screen.getByRole('table')).toBeInTheDocument();
      expect(screen.getByText('North')).toBeInTheDocument();
      unmount();
    }
  });

  it('shows a skeleton while the heavy chart bundle loads', () => {
    render(
      <ChartView
        slide={{
          id: 'bar-0',
          type: 'bar',
          title: 'bar',
          spec: { type: 'bar' },
          data: { labels: ['a'], datasets: [{ label: 'v', data: [1] }] },
        }}
      />,
    );
    // lazy() suspends on first render, so the fallback is what is on screen.
    expect(document.querySelectorAll('.skeleton').length).toBeGreaterThan(0);
  });
});
