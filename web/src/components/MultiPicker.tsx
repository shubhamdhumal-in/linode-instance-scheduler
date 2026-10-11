import { useEffect, useRef, useState } from 'react'
export function MultiPicker({
  label,
  options,
  selected,
  onChange,
  placeholder,
  emptyText,
}: {
  label: string
  options: {
    value: string
    hint?: string
  }[]
  selected: string[]
  onChange: (next: string[]) => void
  placeholder: string
  emptyText: string
}) {
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const rootRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!open) return
    const onDoc = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onDoc)
    return () => document.removeEventListener('mousedown', onDoc)
  }, [open])
  const toggle = (value: string) =>
    onChange(selected.includes(value) ? selected.filter((v) => v !== value) : [...selected, value])
  const q = query.trim().toLowerCase()
  const shown = options.filter((o) => !q || o.value.toLowerCase().includes(q))
  return (
    <div className="relative" ref={rootRef}>
      <span className="text-slate-700">{label}</span>
      <button
        type="button"
        aria-label={label}
        aria-expanded={open}
        className="mt-1 flex min-h-[34px] w-full flex-wrap items-center gap-1 rounded-md border border-slate-300 bg-white px-2 py-1 text-left"
        onClick={() => setOpen((v) => !v)}
      >
        {selected.length === 0 && <span className="px-1 text-slate-400">{placeholder}</span>}
        {selected.map((v) => (
          <span
            key={v}
            className="inline-flex items-center gap-1 rounded bg-indigo-50 px-1.5 py-0.5 text-xs text-indigo-800"
          >
            {v}
            <span
              role="button"
              aria-label={`Remove ${v}`}
              className="cursor-pointer text-indigo-500 hover:text-indigo-800"
              onClick={(e) => {
                e.stopPropagation()
                toggle(v)
              }}
            >
              ×
            </span>
          </span>
        ))}
      </button>
      {open && (
        <div className="absolute z-20 mt-1 w-full rounded-md border border-slate-200 bg-white shadow-lg">
          <input
            autoFocus
            aria-label={`Search ${label}`}
            className="block w-full border-b border-slate-200 px-3 py-1.5 text-sm outline-none"
            placeholder="Search…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          <ul className="max-h-56 overflow-auto py-1" role="listbox" aria-multiselectable="true">
            {options.length === 0 && <li className="px-3 py-2 text-xs text-slate-500">{emptyText}</li>}
            {options.length > 0 && shown.length === 0 && (
              <li className="px-3 py-2 text-xs text-slate-500">No match</li>
            )}
            {shown.map((o) => (
              <li key={o.value} role="option" aria-selected={selected.includes(o.value)}>
                <label className="flex cursor-pointer items-center gap-2 px-3 py-1 hover:bg-slate-50">
                  <input
                    type="checkbox"
                    checked={selected.includes(o.value)}
                    onChange={() => toggle(o.value)}
                  />
                  <span className="text-slate-900">{o.value}</span>
                  {o.hint && <span className="text-xs text-slate-400">{o.hint}</span>}
                </label>
              </li>
            ))}
          </ul>
          <div className="flex justify-between border-t border-slate-100 px-3 py-1.5 text-xs">
            <button
              type="button"
              className="text-slate-500 hover:text-slate-800"
              onClick={() => onChange([])}
            >
              Clear
            </button>
            <button type="button" className="font-medium text-indigo-600" onClick={() => setOpen(false)}>
              Done
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
