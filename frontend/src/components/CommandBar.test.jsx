import { describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import CommandBar from './CommandBar';
import { api } from '../lib/api';

const dataset = { dataset_id: 'ds1', name: 'sales.xlsx' };
const sheet = { name: 'Transactions' };
const slide = { id: 'nl-1', type: 'treemap', title: 'Revenue by region', spec: { type: 'treemap' }, data: {} };

function setup(overrides = {}) {
  vi.spyOn(api, 'nlSuggestions').mockResolvedValue({
    suggestions: ['total revenue by region', 'revenue over date'],
  });
  const onSlide = vi.fn();
  render(<CommandBar dataset={dataset} sheet={sheet} onSlide={onSlide} llm {...overrides} />);
  return { onSlide };
}

describe('CommandBar', () => {
  it('loads suggestion chips for the active sheet', async () => {
    setup();
    await waitFor(() => expect(screen.getAllByTestId('chip-nl-suggestion')).toHaveLength(2));
    expect(api.nlSuggestions).toHaveBeenCalledWith('ds1', 'Transactions');
  });

  it('submits the prompt and hands the slide back', async () => {
    const nl = vi.spyOn(api, 'nlChart').mockResolvedValue({ slide, source: 'llm', llm: true });
    const { onSlide } = setup();
    await userEvent.type(screen.getByTestId('input-nl-prompt'), 'revenue by region as a treemap');
    await userEvent.click(screen.getByTestId('button-nl-run'));
    await waitFor(() => expect(onSlide).toHaveBeenCalledWith(slide, expect.objectContaining({ source: 'llm' })));
    expect(nl.mock.calls[0][0]).toEqual({
      dataset_id: 'ds1',
      sheet: 'Transactions',
      prompt: 'revenue by region as a treemap',
    });
    expect(await screen.findByTestId('nl-note')).toHaveTextContent(/Interpreted by the model/);
  });

  it('clicking a chip runs that prompt', async () => {
    vi.spyOn(api, 'nlChart').mockResolvedValue({ slide, source: 'keyword', llm: false });
    const { onSlide } = setup();
    const chips = await screen.findAllByTestId('chip-nl-suggestion');
    await userEvent.click(chips[0]);
    await waitFor(() => expect(onSlide).toHaveBeenCalled());
    expect(api.nlChart.mock.calls[0][0].prompt).toBe('total revenue by region');
  });

  it('reports the keyword fallback in human terms', async () => {
    vi.spyOn(api, 'nlChart').mockResolvedValue({
      slide,
      source: 'keyword',
      llm: true,
      warning: 'The model reply was rejected twice; used a keyword match.',
    });
    setup();
    await userEvent.type(screen.getByTestId('input-nl-prompt'), 'nonsense');
    await userEvent.click(screen.getByTestId('button-nl-run'));
    expect(await screen.findByTestId('nl-note')).toHaveTextContent(/keywords instead/);
  });

  it('never leaks raw provider errors into the note', async () => {
    vi.spyOn(api, 'nlChart').mockResolvedValue({
      slide,
      source: 'keyword',
      llm: true,
      warning: "Model output rejected (Error code: 401 - {'type': 'authentication_error'}).",
    });
    setup();
    await userEvent.type(screen.getByTestId('input-nl-prompt'), 'anything');
    await userEvent.click(screen.getByTestId('button-nl-run'));
    const note = await screen.findByTestId('nl-note');
    expect(note).toHaveTextContent('Model unavailable — matched by keywords instead.');
    expect(note.textContent).not.toMatch(/401|authentication_error/);
  });

  it('shows an inline error when the request fails', async () => {
    vi.spyOn(api, 'nlChart').mockRejectedValue(new Error('The prompt is too long.'));
    setup();
    await userEvent.type(screen.getByTestId('input-nl-prompt'), 'x'.repeat(20));
    await userEvent.click(screen.getByTestId('button-nl-run'));
    expect(await screen.findByTestId('nl-error')).toHaveTextContent('The prompt is too long.');
  });

  it('offers a cancel button while in flight and aborts the request', async () => {
    let abortSignal;
    vi.spyOn(api, 'nlChart').mockImplementation(
      (_body, signal) =>
        new Promise((_resolve, reject) => {
          abortSignal = signal;
          signal.addEventListener('abort', () => {
            const err = new Error('aborted');
            err.name = 'AbortError';
            reject(err);
          });
        }),
    );
    const { onSlide } = setup();
    await userEvent.type(screen.getByTestId('input-nl-prompt'), 'revenue by region');
    await userEvent.click(screen.getByTestId('button-nl-run'));
    const cancel = await screen.findByTestId('button-nl-cancel');
    await userEvent.click(cancel);
    await waitFor(() => expect(abortSignal.aborted).toBe(true));
    expect(onSlide).not.toHaveBeenCalled();
    expect(screen.queryByTestId('nl-error')).not.toBeInTheDocument();
  });

  it('notes keyword-only mode when no model key is configured', async () => {
    setup({ llm: false });
    expect(await screen.findByText(/keyword mode/i)).toBeInTheDocument();
  });

  it('disables the run button until something is typed', async () => {
    setup();
    expect(screen.getByTestId('button-nl-run')).toBeDisabled();
    await userEvent.type(screen.getByTestId('input-nl-prompt'), 'a');
    expect(screen.getByTestId('button-nl-run')).toBeEnabled();
  });
});
