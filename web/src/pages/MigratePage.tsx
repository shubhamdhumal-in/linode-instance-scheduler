import { useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { api, ApiError, errorWarnings } from '../api/client'
import type {
  AssistedCopyReadiness,
  BackupEstimate,
  MigrateAbandonResult,
  MigrateResumeResult,
} from '../api/types'
import {
  Button,
  Card,
  ErrorBanner,
  ProgressBar,
  Spinner,
  TypeToConfirmInput,
  typedConfirmationMatches,
  WarningBanner,
} from '../components/ui'
import {
  buildDdCommand,
  guessSourceAndDestination,
  parseLsblkDevices,
  verifyDdOutput,
  type LsblkDevice,
} from '../lib/ddVerify'
import { useSshCredentials } from '../hooks/useSshCredentials'
import { useStatusBar } from '../status/StatusBarContext'
import { PageHeader } from './DashboardLayout'
type Step =
  | 'loading'
  | 'start'
  | 'starting'
  | 'manual'
  | 'resuming'
  | 'onboarding'
  | 'done'
  | 'error'
  | 'incomplete'
  | 'setup_incomplete'
  | 'stale_checkpoint'
  | 'live'
  | 'reconciled'
  | 'cancelled'
type ManualPhase = 'lsblk' | 'confirm_devices' | 'run_dd'
export function MigratePage() {
  const [searchParams] = useSearchParams()
  const navigate = useNavigate()
  const statusBar = useStatusBar()
  const name = searchParams.get('name') ?? ''
  const instanceIdParam = searchParams.get('instanceId')
  const instanceId = instanceIdParam ? Number(instanceIdParam) : null
  const force = searchParams.get('force') === '1'
  const migrationKey = `migrate:${name}:${instanceId ?? ''}`
  const trackedEntry = statusBar.findByKey(migrationKey)
  const migrationPath =
    `/migrate?name=${encodeURIComponent(name)}&instanceId=${instanceId ?? ''}` + (force ? '&force=1' : '')
  const returnToThisMigration = () => navigate(migrationPath)
  const [step, setStep] = useState<Step>('loading')
  const [restart, setRestart] = useState<{
    previousInstanceId: number | null
  } | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [migrateWarnings, setMigrateWarnings] = useState<string[]>([])
  const [onboardSuccessWarnings, setOnboardSuccessWarnings] = useState<string[] | null>(null)
  const [progress, setProgress] = useState<{
    percent: number
    label: string | null
  } | null>(null)
  const [resolvedInstanceId, setResolvedInstanceId] = useState<number | null>(instanceId)
  const [destVolumeSizeGb, setDestVolumeSizeGb] = useState<number | null>(null)
  const [localDiskSizeMb, setLocalDiskSizeMb] = useState<number | null>(null)
  const [manualPhase, setManualPhase] = useState<ManualPhase>('lsblk')
  const [lsblkOutput, setLsblkOutput] = useState('')
  const [selectedSource, setSelectedSource] = useState('')
  const [selectedDest, setSelectedDest] = useState('')
  const [ddOutput, setDdOutput] = useState('')
  const [confirmName, setConfirmName] = useState('')
  const [keepBackup, setKeepBackup] = useState(false)
  const [assisted, setAssisted] = useState<AssistedCopyReadiness | null>(null)
  const [assistedChecking, setAssistedChecking] = useState(false)
  const [assistedCopyDone, setAssistedCopyDone] = useState(false)
  const [assistedRunning, setAssistedRunning] = useState(false)
  const [backupEstimate, setBackupEstimate] = useState<BackupEstimate | 'unavailable' | null>(null)
  const [copyState, setCopyState] = useState<{
    text: string
    status: 'copied' | 'failed'
  } | null>(null)
  const copyResetTimeoutRef = useRef<number | null>(null)
  const [onboardError, setOnboardError] = useState<string | null>(null)
  const [onboardErrorWarnings, setOnboardErrorWarnings] = useState<string[] | null>(null)
  const ssh = useSshCredentials(instanceId ?? undefined)
  const currentKeyRef = useRef<string | null>(migrationKey)
  const [confirmCancel, setConfirmCancel] = useState(false)
  const [cancelling, setCancelling] = useState(false)
  const [cancelResult, setCancelResult] = useState<MigrateAbandonResult | null>(null)
  useEffect(() => {
    currentKeyRef.current = migrationKey
    setAssisted(null)
    setAssistedCopyDone(false)
    setConfirmCancel(false)
    setCancelResult(null)
    return () => {
      currentKeyRef.current = null
    }
  }, [migrationKey])
  const checkResumability = () => {
    const requestedKey = migrationKey
    api
      .getMigrateStatus(name)
      .then((status) => {
        if (requestedKey !== currentKeyRef.current) return
        if (
          status.in_progress &&
          instanceId !== null &&
          status.instance_id != null &&
          status.instance_id !== instanceId
        ) {
          setResolvedInstanceId(status.instance_id)
          setStep('stale_checkpoint')
        } else if (status.in_progress && status.phase === 'awaiting_manual_dd') {
          setResolvedInstanceId(status.instance_id ?? instanceId)
          setDestVolumeSizeGb(status.dest_volume_size_gb ?? null)
          setLocalDiskSizeMb(status.local_disk_size_mb ?? null)
          setStep('manual')
        } else if (status.in_progress) {
          setStep('setup_incomplete')
        } else if (instanceId === null) {
          setStep('error')
          setError('No instance selected -- go back to Onboard and try again.')
        } else {
          setStep('start')
        }
      })
      .catch((e) => {
        if (requestedKey !== currentKeyRef.current) return
        setStep('error')
        setError(e instanceof ApiError ? e.message : 'Could not check migration status.')
      })
  }
  useEffect(() => {
    setStep('loading')
    setRestart(null)
    ssh.reset()
    setResolvedInstanceId(instanceId)
    setDestVolumeSizeGb(null)
    setLocalDiskSizeMb(null)
    setManualPhase('lsblk')
    setLsblkOutput('')
    setSelectedSource('')
    setSelectedDest('')
    setDdOutput('')
    setConfirmName('')
    setKeepBackup(false)
    setBackupEstimate(null)
    setOnboardError(null)
    setOnboardErrorWarnings(null)
    setMigrateWarnings([])
    setOnboardSuccessWarnings(null)
    if (!name) {
      setStep('error')
      setError('No instance name given -- go back to Onboard and try again.')
      return
    }
    if (trackedEntry) {
      setStep('live')
      return
    }
    checkResumability()
  }, [name, instanceId])
  useEffect(() => {
    if (step !== 'live') return
    const current = trackedEntry
    if (current === null || current.kind === 'pending') {
      if (current !== null) return
      checkResumability()
      return
    }
    if (current.kind === 'error') {
      if (current.phase === 'finish') {
        setOnboardError(current.detail ?? 'Could not finish onboarding automatically.')
        setOnboardErrorWarnings(null)
        setStep('done')
        return
      }
      setError(current.detail ?? 'Migration failed.')
      if (current.phase === 'resume') {
        checkResumability()
      } else {
        setStep('start')
      }
      return
    }
    if (current.phase === 'start') {
      checkResumability()
      return
    }
    const requestedKey = migrationKey
    api.getInstance(name).then(
      () => {
        if (requestedKey === currentKeyRef.current) setStep('reconciled')
      },
      () => {
        if (requestedKey !== currentKeyRef.current) return
        if (current.phase === 'resume') {
          finishOnboarding()
          return
        }
        checkResumability()
      },
    )
  }, [step, trackedEntry])
  const startMigration = async () => {
    if (instanceId === null) return
    const requestedKey = migrationKey
    setStep('starting')
    setError(null)
    setMigrateWarnings([])
    setProgress({ percent: 0, label: null })
    try {
      const result = await statusBar.run(
        `Migrating "${name}" to Block Storage`,
        (report) =>
          api.migrateStart(
            name,
            instanceId,
            ssh.payload,
            (percent, currentStep) => {
              if (requestedKey === currentKeyRef.current) {
                setProgress({ percent, label: currentStep })
              }
              report(percent, currentStep)
            },
            (warnings) => {
              if (requestedKey === currentKeyRef.current) setMigrateWarnings(warnings)
            },
            restart !== null,
            keepBackup,
          ),
        { onNavigate: returnToThisMigration, viewPath: migrationPath, key: migrationKey, phase: 'start' },
      )
      if (requestedKey !== currentKeyRef.current) return
      ssh.reset()
      setResolvedInstanceId(result.instance_id ?? instanceId)
      setDestVolumeSizeGb(result.dest_volume_size_gb)
      setLocalDiskSizeMb(result.local_disk_size_mb)
      setStep('manual')
    } catch (e) {
      if (requestedKey !== currentKeyRef.current) return
      const carried = errorWarnings(e)
      if (carried) setMigrateWarnings((prev) => [...prev, ...carried])
      const message = e instanceof ApiError ? e.message : 'Could not start migration.'
      setError(message)
      ssh.openIfLikelySshFailure(message)
      setConfirmName('')
      setStep('start')
    } finally {
      if (requestedKey === currentKeyRef.current) setProgress(null)
    }
  }
  const cancelMigration = async () => {
    const requestedKey = migrationKey
    setCancelling(true)
    setError(null)
    try {
      const result = await statusBar.run(`Cancelling the migration of "${name}"`, (report) =>
        api.abandonMigration(name, true, report, (warnings) => {
          if (requestedKey === currentKeyRef.current) setMigrateWarnings(warnings)
        }),
      )
      if (requestedKey !== currentKeyRef.current) return
      setCancelResult(result)
      setStep('cancelled')
    } catch (e) {
      if (requestedKey !== currentKeyRef.current) return
      setError(e instanceof ApiError ? e.message : 'Could not cancel the migration.')
    } finally {
      if (requestedKey === currentKeyRef.current) {
        setCancelling(false)
        setConfirmCancel(false)
      }
    }
  }
  const cancelPanel = (
    <div className="space-y-2 border-t border-slate-200 pt-4">
      {confirmCancel ? (
        <>
          <p className="text-sm text-slate-700">
            Cancel this migration? The original instance leaves Rescue Mode and boots from its own disk,
            unchanged. The destination volume (the copy) is deleted. A pre-migration backup, if one was kept,
            stays until you remove it on the Backups page.
          </p>
          <div className="flex gap-2">
            <Button variant="danger" disabled={cancelling} onClick={() => void cancelMigration()}>
              {cancelling ? 'Cancelling…' : 'Yes, cancel the migration'}
            </Button>
            <Button disabled={cancelling} onClick={() => setConfirmCancel(false)}>
              Keep it
            </Button>
          </div>
        </>
      ) : (
        <Button className="w-full" onClick={() => setConfirmCancel(true)}>
          Cancel this migration
        </Button>
      )}
    </div>
  )
  const checkAssistedCopy = async () => {
    const requestedKey = migrationKey
    setAssistedChecking(true)
    try {
      const r = await api.getAssistedCopy(name)
      if (requestedKey === currentKeyRef.current) setAssisted(r)
    } catch {
      if (requestedKey === currentKeyRef.current) setAssisted(null)
    } finally {
      if (requestedKey === currentKeyRef.current) setAssistedChecking(false)
    }
  }
  useEffect(() => {
    if (step === 'manual' && name) void checkAssistedCopy()
  }, [step, migrationKey])
  const resumeMigration = async (assisted = false) => {
    const requestedKey = migrationKey
    setStep('resuming')
    setError(null)
    setMigrateWarnings([])
    setProgress({ percent: 0, label: null })
    try {
      const onProgress =
        (report: (p: number, s: string | null) => void) => (percent: number, currentStep: string | null) => {
          if (requestedKey === currentKeyRef.current) {
            setProgress({ percent, label: currentStep })
          }
          report(percent, currentStep)
        }
      const onWarning = (warnings: string[]) => {
        if (requestedKey === currentKeyRef.current) {
          setMigrateWarnings((prev) => [...prev, ...warnings])
        }
      }
      let result: MigrateResumeResult
      if (assisted) setAssistedRunning(true)
      if (assisted) {
        const copied = await statusBar.run(
          `Copying and finishing migration for "${name}"`,
          (report) => api.runAssistedCopy(name, onProgress(report), onWarning),
          { onNavigate: returnToThisMigration, viewPath: migrationPath, key: migrationKey, phase: 'resume' },
        )
        if (copied.outcome !== 'resumed' || copied.resume === null) {
          if (requestedKey === currentKeyRef.current) {
            setAssistedCopyDone(true)
            setError(
              `The copy finished, but finishing the migration failed: ${copied.detail ?? 'unknown error'}. ` +
                'Click "Finish migration" to try again.',
            )
            setStep('manual')
          }
          return
        }
        result = copied.resume
      } else {
        result = await statusBar.run(
          `Finishing migration for "${name}"`,
          (report) => api.migrateResume(name, onProgress(report), onWarning),
          { onNavigate: returnToThisMigration, viewPath: migrationPath, key: migrationKey, phase: 'resume' },
        )
      }
      if (result.outcome === 'resumed') {
        const resumeNotes: string[] = []
        if (result.previous_attempts_count > 0) {
          resumeNotes.push(
            `${result.previous_attempts_count} earlier incomplete migration attempt(s) for ` +
              `"${name}" were replaced before this one. Their resource IDs (most importantly ` +
              'any destination volume) are preserved and were NOT deleted automatically -- ' +
              `they are likely still billing. Run \`migrate-orphans --name ${name}\` (CLI) to ` +
              "see them, and `--cleanup` to delete them once you're sure they're no longer needed.",
          )
        }
        if (result.fstab_entries_disabled.length > 0) {
          resumeNotes.push(
            `${result.fstab_entries_disabled.length} stale /etc/fstab entr` +
              `${result.fstab_entries_disabled.length === 1 ? 'y was' : 'ies were'} disabled ` +
              '(device no longer present after migration -- would otherwise stall every future ' +
              'boot). A backup was made before editing.',
          )
        }
        if (resumeNotes.length > 0 && requestedKey === currentKeyRef.current) {
          setMigrateWarnings((prev) => [...prev, ...resumeNotes])
        }
        await finishOnboarding(resumeNotes)
        return
      }
      if (requestedKey !== currentKeyRef.current) return
      setError(
        result.detail ??
          'The migration itself completed, but recording that locally failed. Check the CLI (`migrate-resume`/`onboard`) to finish.',
      )
      setStep('incomplete')
    } catch (e) {
      if (requestedKey !== currentKeyRef.current) return
      const carried = errorWarnings(e)
      if (carried) setMigrateWarnings((prev) => [...prev, ...carried])
      setError(
        e instanceof ApiError
          ? e.message
          : assisted
            ? 'The assisted copy failed.'
            : 'Could not resume migration.',
      )
      setStep('manual')
    } finally {
      if (requestedKey === currentKeyRef.current) {
        setProgress(null)
        setAssistedRunning(false)
      }
    }
  }
  const finishOnboarding = async (migrateResumeNotes: string[] = []) => {
    const requestedKey = migrationKey
    const instId = resolvedInstanceId ?? instanceId
    if (instId === null) {
      setStep('done')
      return
    }
    setStep('onboarding')
    setOnboardError(null)
    setOnboardErrorWarnings(null)
    try {
      const result = await statusBar.run(
        `Finishing onboarding for "${name}"`,
        () => api.onboardInstance({ name, instance_id: instId, force }),
        { onNavigate: returnToThisMigration, viewPath: migrationPath, key: migrationKey, phase: 'finish' },
      )
      if (requestedKey !== currentKeyRef.current) return
      if (result.outcome === 'onboarded') {
        if ((result.warnings && result.warnings.length > 0) || migrateResumeNotes.length > 0) {
          setOnboardSuccessWarnings(result.warnings ?? [])
          setStep('done')
          return
        }
        navigate(`/instances/${encodeURIComponent(name)}`)
        return
      }
      setOnboardError('Onboarding was declined (identity change not confirmed).')
    } catch (e) {
      if (requestedKey !== currentKeyRef.current) return
      setOnboardErrorWarnings(errorWarnings(e))
      setOnboardError(e instanceof ApiError ? e.message : 'Could not finish onboarding automatically.')
    }
    if (requestedKey === currentKeyRef.current) setStep('done')
  }
  useEffect(() => {
    return () => {
      if (copyResetTimeoutRef.current !== null) window.clearTimeout(copyResetTimeoutRef.current)
    }
  }, [])
  const copyText = async (text: string) => {
    if (copyResetTimeoutRef.current !== null) window.clearTimeout(copyResetTimeoutRef.current)
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(text)
      } else {
        const textarea = document.createElement('textarea')
        textarea.value = text
        textarea.style.position = 'fixed'
        textarea.style.left = '-9999px'
        document.body.appendChild(textarea)
        textarea.select()
        const ok = document.execCommand('copy')
        document.body.removeChild(textarea)
        if (!ok) throw new Error('execCommand copy failed')
      }
      setCopyState({ text, status: 'copied' })
      if ('vibrate' in navigator) {
        try {
          navigator.vibrate(15)
        } catch {}
      }
    } catch {
      setCopyState({ text, status: 'failed' })
    }
    copyResetTimeoutRef.current = window.setTimeout(() => setCopyState(null), 2000)
  }
  const parsedDevices: LsblkDevice[] = useMemo(() => parseLsblkDevices(lsblkOutput), [lsblkOutput])
  const deviceGuess = useMemo(
    () => guessSourceAndDestination(parsedDevices, localDiskSizeMb, destVolumeSizeGb),
    [parsedDevices, localDiskSizeMb, destVolumeSizeGb],
  )
  const ddCommand = selectedSource && selectedDest ? buildDdCommand(selectedSource, selectedDest) : null
  const ddOutputVerification = useMemo(
    () =>
      selectedSource && selectedDest
        ? verifyDdOutput(ddOutput, selectedSource, selectedDest)
        : { status: 'inconclusive' as const },
    [ddOutput, selectedSource, selectedDest],
  )
  const goToConfirmDevices = () => {
    setSelectedSource((current) => current || (deviceGuess.source ?? ''))
    setSelectedDest((current) => current || (deviceGuess.destination ?? ''))
    setManualPhase('confirm_devices')
  }
  return (
    <div>
      <PageHeader title="Migrate onto Block Storage" subtitle={name ? `For “${name}”` : undefined} />
      <div className="mx-auto max-w-2xl p-8">
        <Card>
          <div className="space-y-5 px-6 py-6">
            {step === 'loading' && (
              <div className="flex justify-center py-8">
                <Spinner className="text-indigo-600" />
              </div>
            )}

            {step === 'error' && (
              <>
                {error && <ErrorBanner message={error} />}
                <Button variant="secondary" className="w-full" onClick={() => navigate('/onboard')}>
                  Back to Onboard
                </Button>
              </>
            )}

            {step === 'live' && (
              <>
                <p className="text-sm text-slate-700">
                  A migration for “{name}” is already in progress — reconnected to its real, live status
                  below.
                </p>
                {trackedEntry && typeof trackedEntry.percent === 'number' && (
                  <ProgressBar percent={trackedEntry.percent} label={trackedEntry.detail ?? null} />
                )}
              </>
            )}

            {step === 'reconciled' && (
              <>
                <div className="rounded-md bg-emerald-50 px-3 py-3 text-sm text-emerald-800 ring-1 ring-inset ring-emerald-200">
                  Migration and onboarding for “{name}” completed successfully while this page wasn't open —
                  some details from that run (e.g. an identity-change notice from a force re-onboard) may not
                  be shown here. Check the instance's own page for its current status.
                </div>
                <Button
                  variant="primary"
                  className="w-full"
                  onClick={() => navigate(`/instances/${encodeURIComponent(name)}`)}
                >
                  Continue to instance
                </Button>
              </>
            )}

            {step === 'start' && (
              <>
                <div className="space-y-2 text-sm text-slate-700">
                  <p>
                    This instance's OS is currently on <strong>local disk</strong>, not a Block Storage
                    volume. This tool works by deleting and recreating the instance on every stop/start cycle
                    — local disk doesn't survive that, a Block Storage volume does.
                  </p>
                  <p>
                    This is a <strong>one-time migration</strong>. Almost everything is automated; there's
                    exactly one manual step (copying the disk yourself, in a Lish console) that genuinely
                    can't be done through the API. We'll walk you through it.
                  </p>
                </div>

                <div className="rounded-md bg-red-50 px-3 py-3 text-sm text-red-800 ring-1 ring-inset ring-red-200">
                  <p className="font-medium">This causes real downtime.</p>
                  <p className="mt-1">
                    In a moment, this instance reboots into Rescue Mode and is{' '}
                    <strong>completely unavailable</strong> for the entire duration of the manual copy step
                    below — which can take anywhere from a few minutes to well over an hour depending on how
                    much data is on it — and again briefly when it reboots back afterward. Plan this for a
                    scheduled maintenance window or off-hours, not while this instance is serving live
                    traffic.
                  </p>
                </div>

                {error && <ErrorBanner message={error} />}
                {restart !== null && (
                  <div className="rounded-md bg-slate-50 px-3 py-2.5 text-xs text-slate-600">
                    This replaces the earlier migration attempt
                    {restart.previousInstanceId !== null && restart.previousInstanceId !== instanceId
                      ? ` for instance ${restart.previousInstanceId}`
                      : ''}
                    .
                  </div>
                )}
                {ssh.node}

                <div className="rounded-md bg-slate-50 px-3 py-3 text-sm text-slate-700 ring-1 ring-inset ring-slate-200">
                  <label className="flex items-start gap-2">
                    <input
                      type="checkbox"
                      className="mt-0.5"
                      checked={keepBackup}
                      onChange={(e) => {
                        const on = e.target.checked
                        setKeepBackup(on)
                        if (on && backupEstimate === null && instanceId !== null) {
                          api.getBackupEstimate(instanceId).then(
                            (est) => setBackupEstimate(est),
                            () => setBackupEstimate('unavailable'),
                          )
                        }
                      }}
                    />
                    <span>
                      <strong>Keep a backup of the original first</strong> (recommended). The instance is
                      powered off and Linode clones it and its attached volumes. The clone stays powered off,
                      and <em>Backups → Roll back</em> puts the original system back if anything goes wrong.
                    </span>
                  </label>
                  {keepBackup && (
                    <p className="mt-2 text-xs text-amber-800">
                      This creates billable resources until you delete the backup
                      {backupEstimate && backupEstimate !== 'unavailable'
                        ? ` — about $${backupEstimate.total_monthly.toFixed(2)}/month at Linode's current prices ` +
                          `($${backupEstimate.instance_monthly.toFixed(2)} for the powered-off ${backupEstimate.plan} clone, ` +
                          `$${backupEstimate.volumes_monthly.toFixed(2)} for ${backupEstimate.volume_gb} GB of cloned volumes)`
                        : ' (a powered-off Linode still bills at its plan price, cloned volumes as Block Storage)'}
                      . Cloning adds a few minutes before the copy step.
                    </p>
                  )}
                </div>

                <TypeToConfirmInput
                  expected={name}
                  value={confirmName}
                  onChange={setConfirmName}
                  ringClassName="focus:ring-red-600"
                  label={<>Type “{name}” to confirm you understand this instance will go down now</>}
                />

                <Button
                  variant="danger"
                  className="w-full"
                  disabled={!ssh.isValid || !typedConfirmationMatches(confirmName, name)}
                  onClick={startMigration}
                >
                  Start migration now
                </Button>
              </>
            )}

            {step === 'starting' && (
              <>
                <p className="text-sm text-slate-700">
                  Creating the destination volume and preparing Rescue Mode — this involves a few real API
                  calls and can take a little while.
                </p>
                {progress && <ProgressBar percent={progress.percent} label={progress.label} />}
              </>
            )}

            {(step === 'manual' || step === 'resuming') && (
              <>
                {migrateWarnings.length > 0 && <WarningBanner messages={migrateWarnings} />}
                {(destVolumeSizeGb !== null || localDiskSizeMb !== null) && (
                  <div className="rounded-md bg-slate-50 px-3 py-2.5 text-xs text-slate-600">
                    <p>
                      Source (local disk): <strong>~{localDiskSizeMb ?? '?'} MB</strong>
                    </p>
                    <p>
                      Destination (new Block Storage volume): <strong>~{destVolumeSizeGb ?? '?'} GB</strong>
                    </p>
                    <p className="mt-1.5">
                      This copies the full disk, not just used space — depending on your account's disk
                      throughput, that can take anywhere from a few minutes to well over an hour for a larger
                      disk. Please be patient and let it finish cleanly rather than interrupting it.
                    </p>
                  </div>
                )}

                {step === 'resuming' && assistedRunning && (
                  <div className="rounded-md border border-indigo-200 bg-indigo-50 p-4 space-y-3 text-sm">
                    <p className="font-medium text-slate-900">
                      The tool is running the copy in the Rescue Mode console
                    </p>
                    <p className="text-slate-700">
                      This takes a few minutes for a typical disk, longer for a large one. When it finishes,
                      the migration completes and the instance is onboarded.
                    </p>
                    {progress && <ProgressBar percent={progress.percent} label={progress.label} />}
                    <a
                      className="text-indigo-700 underline"
                      href={`#/logs?log=lish-${encodeURIComponent(name)}`}
                    >
                      Watch the console session
                    </a>
                  </div>
                )}
                {step === 'manual' && assistedCopyDone && (
                  <div className="rounded-md border border-emerald-300 bg-emerald-50 p-4 space-y-2 text-sm">
                    <p className="text-emerald-900">The copy already finished. Only the last step is left.</p>
                    <Button variant="primary" onClick={() => resumeMigration()}>
                      Finish migration
                    </Button>
                  </div>
                )}
                {step === 'manual' && !assistedCopyDone && assisted?.backup_kept && (
                  <div className="rounded-md border border-indigo-200 bg-indigo-50 p-4 space-y-3 text-sm">
                    <p className="font-medium text-slate-900">Let the tool run the copy for you</p>
                    <p className="text-slate-700">
                      A backup of this instance is kept, so you can roll back whatever happens. The tool opens
                      this instance's Rescue Mode console over Lish, runs the same copy command shown below,
                      waits for it to finish, then finishes the migration and onboards the instance.
                    </p>
                    {assisted.available ? (
                      <Button variant="primary" onClick={() => resumeMigration(true)}>
                        Run the copy for me
                      </Button>
                    ) : (
                      <div className="space-y-2">
                        <p className="text-slate-700">
                          First, let this deployment's key log in to Lish: in Cloud Manager open{' '}
                          <strong>Profile → LISH Console Settings</strong>, allow key login, and add this
                          public key
                          {assisted.username ? (
                            <>
                              {' '}
                              (Lish user <strong>{assisted.username}</strong>)
                            </>
                          ) : null}
                          :
                        </p>
                        {assisted.public_key && (
                          <pre className="overflow-x-auto rounded bg-white p-2 text-xs text-slate-800">
                            {assisted.public_key}
                          </pre>
                        )}
                        <ul className="list-disc pl-5 text-xs text-slate-600">
                          {assisted.reasons.map((r) => (
                            <li key={r}>{r}</li>
                          ))}
                        </ul>
                        <Button
                          variant="secondary"
                          disabled={assistedChecking}
                          onClick={() => void checkAssistedCopy()}
                        >
                          {assistedChecking ? 'Checking…' : 'Check again'}
                        </Button>
                      </div>
                    )}
                    <p className="text-xs text-slate-500">Or do it yourself with the steps below.</p>
                  </div>
                )}

                {manualPhase === 'lsblk' && step === 'manual' && (
                  <div className="space-y-3 text-sm text-slate-700">
                    <p className="font-medium text-slate-900">Step 1 of 3 — identify the devices</p>
                    <ol className="list-decimal space-y-1.5 pl-5">
                      <li>
                        In Cloud Manager, open instance <strong>{resolvedInstanceId ?? instanceId}</strong>{' '}
                        and click "Launch LISH Console."
                      </li>
                      <li>Log in as root.</li>
                      <li>
                        Run <code className="rounded bg-slate-100 px-1 py-0.5">lsblk</code> and paste its full
                        output below — we'll match devices to the sizes above automatically, so you never have
                        to trust a fixed device letter.
                      </li>
                    </ol>
                    <label className="block">
                      <span className="text-xs font-medium text-slate-500">lsblk output</span>
                      <textarea
                        className="mt-1 block w-full rounded-md border-0 py-1.5 px-3 font-mono text-xs text-slate-900 ring-1 ring-inset ring-slate-300 focus:ring-2 focus:ring-indigo-600"
                        rows={5}
                        placeholder={
                          'NAME   MAJ:MIN RM  SIZE RO TYPE MOUNTPOINTS\nsda      8:0    0   20G  0 disk\nsdb      8:16   0   25G  0 disk'
                        }
                        value={lsblkOutput}
                        onChange={(e) => setLsblkOutput(e.target.value)}
                      />
                    </label>
                    {lsblkOutput.trim() && parsedDevices.length < 2 && (
                      <div className="rounded-md bg-amber-50 px-3 py-2.5 text-xs text-amber-800 ring-1 ring-inset ring-amber-200">
                        Couldn't recognize at least two block devices in this — make sure you pasted the full,
                        unmodified <code>lsblk</code> output (with no extra flags).
                      </div>
                    )}
                    {parsedDevices.length >= 2 && (
                      <p className="text-xs text-slate-500">
                        Found {parsedDevices.length} device{parsedDevices.length === 1 ? '' : 's'}:{' '}
                        {parsedDevices.map((d) => `/dev/${d.name} (${d.type ?? 'unknown'})`).join(', ')}
                      </p>
                    )}
                    <Button
                      variant="primary"
                      className="w-full"
                      disabled={parsedDevices.length < 2}
                      onClick={goToConfirmDevices}
                    >
                      Next — identify source and destination
                    </Button>
                  </div>
                )}

                {manualPhase === 'confirm_devices' && step === 'manual' && (
                  <div className="space-y-3 text-sm text-slate-700">
                    <p className="font-medium text-slate-900">Step 2 of 3 — confirm which device is which</p>
                    {deviceGuess.source && deviceGuess.destination ? (
                      <div className="rounded-md bg-slate-50 px-3 py-2.5 text-xs text-slate-600">
                        Best guess based on the sizes above: <code>/dev/{deviceGuess.source}</code> is your
                        original disk, <code>/dev/{deviceGuess.destination}</code> is the new volume. Double
                        check and change either below if this looks wrong.
                      </div>
                    ) : (
                      <div className="rounded-md bg-amber-50 px-3 py-2.5 text-xs text-amber-800 ring-1 ring-inset ring-amber-200">
                        We couldn't confidently tell which device is which from the sizes alone — please
                        select both manually below.
                      </div>
                    )}
                    <label className="block">
                      <span className="text-xs font-medium text-slate-500">
                        Original disk (source — will be READ from)
                      </span>
                      <select
                        className="mt-1 block w-full rounded-md border-0 py-1.5 px-3 text-sm text-slate-900 ring-1 ring-inset ring-slate-300 focus:ring-2 focus:ring-indigo-600"
                        value={selectedSource}
                        onChange={(e) => setSelectedSource(e.target.value)}
                      >
                        <option value="">Select...</option>
                        {parsedDevices.map((d) => (
                          <option key={d.name} value={d.name}>
                            /dev/{d.name} — {Math.round(d.sizeMb / 1024)}G{d.type ? ` (${d.type})` : ''}
                          </option>
                        ))}
                      </select>
                    </label>
                    <label className="block">
                      <span className="text-xs font-medium text-slate-500">
                        New Block Storage volume (destination — will be WRITTEN to, currently empty)
                      </span>
                      <select
                        className="mt-1 block w-full rounded-md border-0 py-1.5 px-3 text-sm text-slate-900 ring-1 ring-inset ring-slate-300 focus:ring-2 focus:ring-indigo-600"
                        value={selectedDest}
                        onChange={(e) => setSelectedDest(e.target.value)}
                      >
                        <option value="">Select...</option>
                        {parsedDevices.map((d) => (
                          <option key={d.name} value={d.name}>
                            /dev/{d.name} — {Math.round(d.sizeMb / 1024)}G{d.type ? ` (${d.type})` : ''}
                          </option>
                        ))}
                      </select>
                    </label>
                    {selectedSource && selectedDest && selectedSource === selectedDest && (
                      <div className="rounded-md bg-red-50 px-3 py-2.5 text-xs text-red-800 ring-1 ring-inset ring-red-200">
                        Source and destination must be different devices.
                      </div>
                    )}
                    <div className="flex gap-2">
                      <Button variant="secondary" onClick={() => setManualPhase('lsblk')}>
                        ← Back
                      </Button>
                      <Button
                        variant="primary"
                        className="flex-1"
                        disabled={!selectedSource || !selectedDest || selectedSource === selectedDest}
                        onClick={() => setManualPhase('run_dd')}
                      >
                        Build the dd command
                      </Button>
                    </div>
                  </div>
                )}

                {(manualPhase === 'run_dd' || step === 'resuming') && ddCommand && (
                  <div className="space-y-3 text-sm text-slate-700">
                    <p className="font-medium text-slate-900">Step 3 of 3 — run it and confirm</p>
                    <p>
                      Run this exact command in the same Lish session (same window you ran <code>lsblk</code>{' '}
                      in):
                    </p>
                    <div className="flex items-center gap-2">
                      <code className="block flex-1 overflow-x-auto rounded-md bg-slate-900 px-3 py-2 text-xs text-slate-100">
                        {ddCommand}
                      </code>
                      <Button
                        variant="secondary"
                        className={`!px-2.5 !py-1.5 text-xs ${
                          copyState?.text === ddCommand && copyState.status === 'failed'
                            ? '!text-red-700 !ring-red-300'
                            : ''
                        }`}
                        onClick={() => copyText(ddCommand)}
                        disabled={step === 'resuming'}
                      >
                        {copyState?.text === ddCommand
                          ? copyState.status === 'copied'
                            ? 'Copied!'
                            : 'Copy failed'
                          : 'Copy'}
                      </Button>
                    </div>
                    <p>
                      Wait for it to finish cleanly, then paste the FULL terminal output below —{' '}
                      <strong>including the command line itself</strong> (however your terminal echoed it),
                      not just the final "records in/out" summary. We check that the command you actually ran
                      matches the one above, not just that some copy finished cleanly.
                    </p>
                    <label className="block">
                      <span className="text-xs font-medium text-slate-500">
                        Full terminal output (command + result)
                      </span>
                      <textarea
                        className="mt-1 block w-full rounded-md border-0 py-1.5 px-3 font-mono text-xs text-slate-900 ring-1 ring-inset ring-slate-300 focus:ring-2 focus:ring-indigo-600"
                        rows={5}
                        placeholder={`# ${ddCommand ?? ''}\n5120+0 records in\n5120+0 records out\n21474836480 bytes (21 GB, 20 GiB) copied, 120 s, 179 MB/s`}
                        value={ddOutput}
                        onChange={(e) => setDdOutput(e.target.value)}
                        disabled={step === 'resuming'}
                      />
                    </label>
                    {ddOutputVerification.status === 'inconclusive' && ddOutput.trim() && (
                      <div className="rounded-md bg-amber-50 px-3 py-2.5 text-xs text-amber-800 ring-1 ring-inset ring-amber-200">
                        We couldn't find a clear "records in"/"records out" summary in this — make sure you've
                        pasted dd's complete output, ideally including the command line itself.
                      </div>
                    )}

                    {ddOutputVerification.status === 'looks_successful' && (
                      <div className="rounded-md bg-emerald-50 px-3 py-2.5 text-xs text-emerald-800 ring-1 ring-inset ring-emerald-200">
                        ✓ Looks like a clean copy — matching records in/out, no errors detected.
                      </div>
                    )}
                    {ddOutputVerification.status === 'looks_failed' && (
                      <div className="rounded-md bg-red-50 px-3 py-2.5 text-xs text-red-800 ring-1 ring-inset ring-red-200">
                        ⚠ This doesn't look like it finished cleanly: {ddOutputVerification.detail}. Fix the
                        issue and rerun the command above before continuing.
                      </div>
                    )}

                    {error && <ErrorBanner message={error} />}

                    {step === 'resuming' ? (
                      <>
                        <p className="text-sm text-slate-700">
                          Exiting Rescue Mode, booting the real config, and verifying the migrated volume is
                          genuinely serving root...
                        </p>
                        {progress && <ProgressBar percent={progress.percent} label={progress.label} />}
                      </>
                    ) : (
                      <div className="flex gap-2">
                        <Button variant="secondary" onClick={() => setManualPhase('confirm_devices')}>
                          ← Back
                        </Button>
                        <Button
                          variant="primary"
                          className="flex-1"
                          disabled={!ddOutput.trim() || ddOutputVerification.status === 'looks_failed'}
                          onClick={() => resumeMigration()}
                        >
                          I've completed the copy — finish migration
                        </Button>
                      </div>
                    )}
                  </div>
                )}
              </>
            )}

            {step === 'manual' && !assistedRunning && cancelPanel}

            {step === 'incomplete' && (
              <>
                <div className="rounded-md bg-emerald-50 px-3 py-3 text-sm text-emerald-800 ring-1 ring-inset ring-emerald-200">
                  The migration itself completed successfully — the disk copy is done and the instance
                  verified booting from the new Block Storage volume.
                </div>
                {error && <ErrorBanner message={error} />}
                <p className="text-sm text-slate-700">
                  Only recording that locally failed, which this page can't retry safely (re-running the dd
                  copy would be unnecessary and could make things worse). Use the CLI instead:
                </p>
                <code className="block overflow-x-auto rounded-md bg-slate-900 px-3 py-2 text-xs text-slate-100">
                  migrate-resume --name {name}
                </code>
                <Button variant="secondary" className="w-full" onClick={() => navigate('/onboard')}>
                  Back to Onboard
                </Button>
              </>
            )}

            {step === 'setup_incomplete' && (
              <>
                <ErrorBanner message="This migration's setup was interrupted before reaching the manual copy step." />
                <p className="text-sm text-slate-700">
                  The destination volume and Rescue Mode may not be ready, so it can't be resumed. Start over
                  instead: this runs the checks again, creates a fresh destination volume and reboots the
                  instance into Rescue Mode. The earlier attempt's volume is kept and marked as an orphaned
                  migration attempt, so nothing is deleted.
                </p>
                <Button
                  variant="primary"
                  className="w-full"
                  onClick={() => {
                    setRestart({ previousInstanceId: resolvedInstanceId })
                    setStep('start')
                  }}
                >
                  Start over
                </Button>
                <Button variant="secondary" className="w-full" onClick={() => navigate('/onboard')}>
                  Back to Onboard
                </Button>
                {cancelPanel}
              </>
            )}

            {step === 'stale_checkpoint' && (
              <>
                <div className="rounded-md bg-amber-50 px-3 py-3 text-sm text-amber-800 ring-1 ring-inset ring-amber-200">
                  An earlier migration under the name “{name}” is still recorded, but it was for instance{' '}
                  {resolvedInstanceId}, not this one ({instanceId}) — for example, the original instance was
                  deleted and recreated.
                </div>
                <p className="text-sm text-slate-700">
                  Start a new migration for this instance. The earlier attempt's volume is kept and marked as
                  an orphaned migration attempt, so nothing is deleted; you can remove it later with{' '}
                  <code>migrate-orphans --cleanup</code>.
                </p>
                <Button
                  variant="primary"
                  className="w-full"
                  onClick={() => {
                    setRestart({ previousInstanceId: resolvedInstanceId })
                    setResolvedInstanceId(instanceId)
                    setStep('start')
                  }}
                >
                  Start a new migration for this instance
                </Button>
                <Button variant="secondary" className="w-full" onClick={() => navigate('/onboard')}>
                  Back to Onboard
                </Button>
                {cancelPanel}
              </>
            )}

            {step === 'cancelled' && cancelResult && (
              <>
                <div className="rounded-md bg-emerald-50 px-3 py-3 text-sm text-emerald-800 ring-1 ring-inset ring-emerald-200">
                  Migration cancelled.{' '}
                  {cancelResult.instance_state === 'booted'
                    ? `Instance ${cancelResult.instance_id} is running from its original disk again.`
                    : cancelResult.instance_state === 'gone'
                      ? `Instance ${cancelResult.instance_id} no longer exists.`
                      : `Instance ${cancelResult.instance_id}: ${cancelResult.instance_state.replace('_', ' ')}.`}{' '}
                  {cancelResult.volume_state === 'deleted'
                    ? `The copy's volume ${cancelResult.volume_id} was deleted.`
                    : cancelResult.volume_state === 'kept'
                      ? `Volume ${cancelResult.volume_id} was kept (it belongs to something else).`
                      : ''}
                </div>
                {cancelResult.notes.length > 0 && <WarningBanner messages={cancelResult.notes} />}
                {cancelResult.backup_kept && (
                  <p className="text-sm text-slate-700">
                    The pre-migration backup is still kept; remove it on the Backups page when you no longer
                    need it.
                  </p>
                )}
                <Button variant="secondary" className="w-full" onClick={() => navigate('/onboard')}>
                  Back to Onboard
                </Button>
              </>
            )}

            {step === 'onboarding' && (
              <>
                <p className="text-sm text-slate-700">
                  Migration verified — finishing onboarding automatically...
                </p>
                <div className="flex justify-center py-4">
                  <Spinner className="text-indigo-600" />
                </div>
              </>
            )}

            {step === 'done' && (
              <>
                <div className="rounded-md bg-emerald-50 px-3 py-3 text-sm text-emerald-800 ring-1 ring-inset ring-emerald-200">
                  Migration complete — this instance is now running from a Block Storage volume.
                </div>
                {migrateWarnings.length > 0 && <WarningBanner messages={migrateWarnings} />}
                {onboardSuccessWarnings ? (
                  <>
                    <div className="rounded-md bg-emerald-50 px-3 py-3 text-sm text-emerald-800 ring-1 ring-inset ring-emerald-200">
                      Onboarded successfully — but review this first:
                    </div>

                    {onboardSuccessWarnings.length > 0 && <WarningBanner messages={onboardSuccessWarnings} />}
                    <Button
                      variant="primary"
                      className="w-full"
                      onClick={() => navigate(`/instances/${encodeURIComponent(name)}`)}
                    >
                      Continue to instance
                    </Button>
                  </>
                ) : (
                  <>
                    {onboardErrorWarnings && <WarningBanner messages={onboardErrorWarnings} />}
                    {onboardError && <ErrorBanner message={`Automatic onboarding failed: ${onboardError}`} />}
                    {onboardError && (
                      <Button variant="secondary" className="w-full" onClick={() => finishOnboarding()}>
                        Retry now
                      </Button>
                    )}
                    <Button
                      variant="primary"
                      className="w-full"
                      onClick={() =>
                        navigate(
                          `/onboard?resumeName=${encodeURIComponent(name)}&resumeInstanceId=${resolvedInstanceId ?? instanceId ?? ''}` +
                            (force ? '&resumeForce=1' : ''),
                        )
                      }
                    >
                      {onboardError ? 'Finish onboarding manually' : 'Continue to onboarding'}
                    </Button>
                  </>
                )}
              </>
            )}
          </div>
        </Card>
      </div>
    </div>
  )
}
