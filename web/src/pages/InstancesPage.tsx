import { useCallback, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, ApiError } from '../api/client'
import type { InstanceRecord } from '../api/types'
import { MissedScheduleCard } from '../components/MissedScheduleCard'
import { StatusBadge } from '../components/StatusBadge'
import { Button, Card, EmptyState, ErrorBanner, Spinner } from '../components/ui'
import { usePolling } from '../hooks/usePolling'
import { PageHeader } from './DashboardLayout'
const POLL_INTERVAL_MS = 8000
export function InstancesPage() {
  const [instances, setInstances] = useState<Record<string, InstanceRecord> | null>(null)
  const [error, setError] = useState<string | null>(null)
  const latestRequestId = useRef(0)
  const reload = useCallback(() => {
    const requestId = ++latestRequestId.current
    api
      .listInstances()
      .then((r) => {
        if (requestId !== latestRequestId.current) return
        setInstances(r)
        setError(null)
      })
      .catch((e) => {
        if (requestId !== latestRequestId.current) return
        setError(e instanceof ApiError ? e.message : 'Could not load instances.')
      })
  }, [])
  useEffect(reload, [reload])
  usePolling(reload, POLL_INTERVAL_MS)
  return (
    <div>
      <PageHeader
        title="Instances"
        subtitle="Every node onboarded to this deployment."
        action={
          <Link to="/onboard">
            <Button variant="primary">+ Onboard instance</Button>
          </Link>
        }
      />
      <div className="p-8">
        <MissedScheduleCard onApplied={reload} />
        {error && <ErrorBanner message={error} />}
        {!error && instances === null && (
          <div className="flex justify-center py-16">
            <Spinner className="text-indigo-600" />
          </div>
        )}
        {instances && Object.keys(instances).length === 0 && (
          <Card>
            <EmptyState
              title="No instances onboarded yet"
              subtitle="Click “+ Onboard instance” above to bring an existing Linode instance under management."
            />
          </Card>
        )}
        {instances && Object.keys(instances).length > 0 && (
          <Card className="overflow-hidden">
            <table className="min-w-full divide-y divide-slate-200">
              <thead className="bg-slate-50">
                <tr>
                  {['Name', 'Status', 'Region', 'Reserved IP', 'Linode ID'].map((h) => (
                    <th
                      key={h}
                      className="px-5 py-3 text-left text-xs font-semibold uppercase tracking-wide text-slate-500"
                    >
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100 bg-white">
                {Object.entries(instances).map(([name, record]) => (
                  <tr key={name} className="hover:bg-slate-50">
                    <td className="px-5 py-3 text-sm font-medium text-slate-900">
                      <Link
                        to={`/instances/${encodeURIComponent(name)}`}
                        className="text-indigo-600 hover:underline"
                      >
                        {name}
                      </Link>
                    </td>
                    <td className="px-5 py-3 text-sm">
                      <StatusBadge status={record.current_status} locked={record.transitioning} />
                      {record.schedule_mode === 'manual' && (
                        <span className="ml-2 rounded bg-slate-100 px-1.5 py-0.5 text-xs text-slate-600">
                          Manual-only
                        </span>
                      )}
                    </td>
                    <td className="px-5 py-3 text-sm text-slate-600">{record.region ?? '—'}</td>
                    <td className="px-5 py-3 text-sm text-slate-600">{record.reserved_ip ?? '—'}</td>
                    <td className="px-5 py-3 text-sm text-slate-600">{record.current_linode_id ?? '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Card>
        )}
      </div>
    </div>
  )
}
