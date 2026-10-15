import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { AnimatePresence, motion } from 'framer-motion';
import { api } from '../lib/api';
import { Icon, Spinner } from './ui';

/* Natural-language chart command bar.
   Server-side: the prompt is validated against a pydantic ChartSpec and the real
   column list, retried once, then answered by a keyword matcher. Model output is
   never evaluated, so a bad reply degrades to a keyword match instead of failing. */
export default function CommandBar({ dataset, sheet, onSlide, llm, compact = false }) {
  const [prompt, setPrompt] = useState('');
  const [chips, setChips] = useState([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [note, setNote] = useState(null);
  const inputRef = useRef(null);
  const abortRef = useRef(null);
  const debounceRef = useRef(null);

  const datasetId = dataset?.dataset_id;
  const sheetName = sheet?.name;

  useEffect(() => {
    if (!datasetId) return undefined;
    let alive = true;
    api
      .nlSuggestions(datasetId, sheetName)
      .then((r) => alive && setChips(r.suggestions.slice(0, 4)))
      .catch(() => alive && setChips([]));
    return () => {
      alive = false;
    };
  }, [datasetId, sheetName]);

  // cmd/ctrl+K focuses the bar from anywhere.
  useEffect(() => {
    const onKey = (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        inputRef.current?.focus();
        inputRef.current?.select();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  const cancel = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    setBusy(false);
  }, []);

  useEffect(() => () => abortRef.current?.abort(), []);

  const run = useCallback(
    async (text) => {
      const q = (text ?? '').trim();
      if (!q || !datasetId) return;
      clearTimeout(debounceRef.current);
      cancel();
      const controller = new AbortController();
      abortRef.current = controller;
      setBusy(true);
      setError(null);
      setNote(null);
      try {
        const res = await api.nlChart(
          { dataset_id: datasetId, sheet: sheetName, prompt: q },
          controller.signal,
        );
        onSlide?.(res.slide, res);
        setNote(
          res.source === 'keyword'
            ? keywordNote(res.warning)
            : `Interpreted by the model${res.source === 'llm_retry' ? ' after one retry' : ''}.`,
        );
        setPrompt('');
      } catch (err) {
        if (err.name !== 'AbortError') setError(err.message);
      } finally {
        abortRef.current = null;
        setBusy(false);
      }
    },
    [datasetId, sheetName, onSlide, cancel],
  );

  // Debounced submit so holding Enter or fast chip clicks don't stack requests.
  const submit = useCallback(
    (text) => {
      clearTimeout(debounceRef.current);
      debounceRef.current = setTimeout(() => run(text), 180);
    },
    [run],
  );

  useEffect(() => () => clearTimeout(debounceRef.current), []);

  const placeholder = useMemo(
    () => (llm ? 'Ask for a chart — "revenue by region as a treemap"' : 'Describe a chart — "revenue by region as a treemap"'),
    [llm],
  );

  return (
    <div className={compact ? 'w-full' : 'w-full max-w-[880px]'} data-testid="command-bar">
      <form
        onSubmit={(e) => {
          e.preventDefault();
          submit(prompt);
        }}
        className="flex items-center gap-2 rounded-xl border border-line bg-surface/80 p-1.5 pl-3 backdrop-blur-sm focus-within:border-accent/60"
      >
        <span className="text-faint">
          <Icon name="sparkle" size={16} />
        </span>
        <input
          ref={inputRef}
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          placeholder={placeholder}
          aria-label="Describe the chart you want"
          maxLength={500}
          className="min-w-0 flex-1 bg-transparent py-1.5 text-[13px] text-ink outline-none placeholder:text-faint"
          data-testid="input-nl-prompt"
        />
        <kbd className="num hidden shrink-0 rounded border border-line px-1.5 py-0.5 text-[10px] text-faint sm:inline">
          ⌘K
        </kbd>
        {busy ? (
          <button type="button" onClick={cancel} className="btn py-1.5 text-xs" data-testid="button-nl-cancel">
            <Spinner size={13} /> Cancel
          </button>
        ) : (
          <button
            type="submit"
            className="btn btn-primary py-1.5 text-xs"
            disabled={!prompt.trim()}
            data-testid="button-nl-run"
          >
            <Icon name="arrowRight" size={14} /> Add chart
          </button>
        )}
      </form>

      <div className="mt-2 flex flex-wrap items-center gap-1.5">
        {chips.map((c) => (
          <button
            key={c}
            type="button"
            onClick={() => {
              setPrompt(c);
              submit(c);
            }}
            disabled={busy}
            className="rounded-md border border-line/80 bg-panel/60 px-2 py-1 text-[11px] text-muted transition-colors hover:border-accent/50 hover:text-accent disabled:opacity-50"
            data-testid="chip-nl-suggestion"
          >
            {c}
          </button>
        ))}
        {!llm && chips.length > 0 && (
          <span className="text-[10px] text-faint">keyword mode · no model key configured</span>
        )}
      </div>

      <AnimatePresence>
        {(error || note) && (
          <motion.p
            initial={{ opacity: 0, y: -4 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0 }}
            className={`mt-2 text-[11px] ${error ? 'text-warn' : 'text-faint'}`}
            data-testid={error ? 'nl-error' : 'nl-note'}
          >
            {error || note}
          </motion.p>
        )}
      </AnimatePresence>
    </div>
  );
}

/* Server warnings can carry raw provider errors; keep the UI note human. */
function keywordNote(warning) {
  if (!warning) return 'Matched by keywords (no model call).';
  if (/error code|authentication|timeout|connection/i.test(warning)) {
    return 'Model unavailable — matched by keywords instead.';
  }
  if (/rejected/i.test(warning)) {
    return 'Model reply failed validation — matched by keywords instead.';
  }
  return warning;
}
