import { useCallback, useEffect, useRef, useState } from 'react'
import { api, ApiError } from '../api/client'
import type { CatchUpResult, ScheduleMismatch } from '../api/types'
import { usePolling } from '../hooks/usePolling'
import { useStatusBar } from '../status/StatusBarContext'
import { Button, Card, ErrorBanner } from './ui'
const POLL_INTERVAL_MS = 60000
export function MissedScheduleCard({ onApplied }: { onApplied?: () => void }) {
  const statusBar = useStatusBar()
  const [mismatches, setMismatches] = useState<ScheduleMismatch[]>([])
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [confirming, setConfirming] = useState(false)
  const [applying, setApplying] = useState(false)
  const [result, setResult] = useState<CatchUpResult | null>(null)
  const [error, setError] = useState<string | null>(null)
  const seen = useRef<Set<string>>(new Set())
  const reload = useCallback(() => {
    api
      .getScheduleMismatches()
      .then((r) => {
        setMismatches(r.mismatches)
        const fresh = r.mismatches.filter((m) => !seen.current.has(m.name))
        for (const m of fresh) seen.current.add(m.name)
        setSelected((prev) => {
          const next = new Set([...prev].filter((n) => r.mismatches.some((m) => m.name === n)))
          for (const m of fresh) if (!m.changed_by_hand) next.add(m.name)
          return next
        })
      })
      .catch(() => {})
  }, [])
  useEffect(reload, [reload])
  usePolling(reload, POLL_INTERVAL_MS, !applying)
  if (mismatches.length === 0 && !result) return null
  const apply = async () => {
    const names = [...selected]
    setConfirming(false)
    setApplying(true)
    setError(null)
    try {
      const r = await statusBar.run(`Catching up ${names.length} node(s) with their schedule`, (report) =>
        api.applyScheduleCatchUp(names, report),
      )
      setResult(r)
      onApplied?.()
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Could not apply the schedule.')
    } finally {
      setApplying(false)
      reload()
    }
  }
  const toggle = (name: string) =>
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(name)) next.delete(name)
      else next.add(name)
      return next
    })
  return (
    <Card className="mb-6 overflow-hidden ring-amber-300">
      <div className="space-y-3 bg-amber-50 px-5 py-4 text-sm text-amber-900">
        {mismatches.length > 0 && (
          <>
            <p className="font-semibold">
              {mismatches.length} node{mismatches.length === 1 ? '' : 's'} not in the state their schedule
              wants
            </p>
            <p>
              The scheduler catches up on a missed start or stop for an hour; after a longer outage (for
              example while the scheduler host was being replaced) these were left as they are. Choose which
              to bring back in line.
            </p>
            <ul className="space-y-1">
              {mismatches.map((m) => (
                <li key={m.name} className="flex items-start gap-2">
                  <input
                    type="checkbox"
                    aria-label={`Catch up ${m.name}`}
                    className="mt-0.5"
                    checked={selected.has(m.name)}
                    disabled={applying}
                    onChange={() => toggle(m.name)}
                  />
                  <span>
                    <strong>{m.name}</strong>
                    {m.via_group ? ` (group ${m.via_group})` : ''}: {m.current} → should be{' '}
                    <strong>{m.desired}</strong>. {m.reason}.
                  </span>
                </li>
              ))}
            </ul>
            {confirming ? (
              <div className="flex flex-wrap items-center gap-2">
                <span>
                  {[...selected]
                    .map((n) => {
                      const m = mismatches.find((x) => x.name === n)
                      return `${m?.desired === 'running' ? 'start' : 'stop'} ${n}`
                    })
                    .join(', ')}
                  ?
                </span>
                <Button variant="primary" disabled={applying} onClick={() => void apply()}>
                  Yes, apply
                </Button>
                <Button disabled={applying} onClick={() => setConfirming(false)}>
                  Cancel
                </Button>
              </div>
            ) : (
              <Button
                variant="primary"
                disabled={applying || selected.size === 0}
                onClick={() => setConfirming(true)}
              >
                {applying ? 'Applying…' : `Apply schedule to ${selected.size} selected`}
              </Button>
            )}
          </>
        )}
        {error && <ErrorBanner message={error} />}
        {result && (
          <div className="space-y-1">
            <p className="font-semibold">Last catch-up:</p>
            <ul>
              {result.items.map((i) => (
                <li key={i.name}>
                  {i.name}: {i.action} → {i.outcome}
                  {i.detail ? ` (${i.detail})` : ''}
                  {i.ok ? '' : ' — failed'}
                </li>
              ))}
            </ul>
            <Button onClick={() => setResult(null)}>Dismiss</Button>
          </div>
        )}
      </div>
    </Card>
  )
}
