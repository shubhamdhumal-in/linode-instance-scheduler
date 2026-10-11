import { expect, test } from '@playwright/test'
import type { Route } from '@playwright/test'
import { baseInstanceRecord, loginAs, mockGroupList, mockInstanceList } from './mocks'
test('a stuck in-progress mark can be cleared from the instance page, after confirming', async ({ page }) => {
  await loginAs(page)
  await mockInstanceList(page, { 'redis-1': baseInstanceRecord({ transitioning: true }) })
  await mockGroupList(page)
  let cleared = 0
  await page.route('**/instances/redis-1/clear-lock', async (r: Route) => {
    cleared += 1
    await r.fulfill({ json: { name: 'redis-1', outcome: 'cleared' } })
  })
  await page.goto('/ui/#/instances/redis-1')
  await expect(page.locator('main')).toContainText('A start or stop is marked as in progress')
  await page.getByRole('button', { name: 'Clear lock' }).click()
  expect(cleared).toBe(0)
  await page.getByRole('button', { name: 'Yes, clear the mark' }).click()
  await expect.poll(() => cleared).toBe(1)
})
test('clearing is refused while a start or stop really is running', async ({ page }) => {
  await loginAs(page)
  await mockInstanceList(page, { 'redis-1': baseInstanceRecord({ transitioning: true }) })
  await mockGroupList(page)
  await page.route('**/instances/redis-1/clear-lock', (r: Route) =>
    r.fulfill({
      status: 409,
      json: { detail: "Another start/stop/rebuild/clear-lock process is already operating on 'redis-1'." },
    }),
  )
  await page.goto('/ui/#/instances/redis-1')
  await page.getByRole('button', { name: 'Clear lock' }).click()
  await page.getByRole('button', { name: 'Yes, clear the mark' }).click()
  await expect(page.locator('main')).toContainText('already operating')
})
test('an unfinished migration can be cancelled from the wizard', async ({ page }) => {
  await loginAs(page)
  await mockInstanceList(page, {})
  await mockGroupList(page)
  await page.route('**/instances/web1/migrate-status', (r: Route) =>
    r.fulfill({ json: { in_progress: true, phase: 'volume_created', instance_id: 5 } }),
  )
  let body: Record<string, unknown> | null = null
  await page.route('**/instances/web1/migrate-abandon', async (r: Route) => {
    body = r.request().postDataJSON()
    await r.fulfill({ json: { operation_id: 'op-a', total_steps: 4 } })
  })
  await page.route('**/operations/op-a', (r: Route) =>
    r.fulfill({
      json: {
        status: 'done',
        action: 'migrate_abandon',
        percent: 100,
        current_step: null,
        warnings: [],
        result: {
          instance_id: 5,
          instance_state: 'booted',
          volume_id: 60,
          volume_state: 'deleted',
          previous_attempts_archived: 0,
          backup_kept: true,
          notes: [],
        },
      },
    }),
  )
  await page.goto('/ui/#/migrate?name=web1&instanceId=5')
  await page.getByRole('button', { name: 'Cancel this migration' }).click()
  await expect(page.locator('main')).toContainText('boots from its own disk, unchanged')
  await page.getByRole('button', { name: 'Yes, cancel the migration' }).click()
  await expect(page.locator('main')).toContainText('Instance 5 is running from its original disk again')
  await expect(page.locator('main')).toContainText("The copy's volume 60 was deleted")
  await expect(page.locator('main')).toContainText('pre-migration backup is still kept')
  expect(body).toMatchObject({ boot_original: true })
})
test('nodes off their schedule are listed and only the reviewed ones are applied', async ({ page }) => {
  await loginAs(page)
  await mockInstanceList(page, {
    'web-1': baseInstanceRecord({ current_status: 'stopped', current_linode_id: null }),
    'web-2': baseInstanceRecord({ current_status: 'running' }),
  })
  await mockGroupList(page)
  await page.route('**/schedule/missed', (r: Route) =>
    r.fulfill({
      json: {
        mismatches: [
          {
            name: 'web-1',
            current: 'stopped',
            desired: 'running',
            via_group: 'dev',
            missed_at: null,
            changed_by_hand: false,
            reason: 'the scheduled start at 2026-10-12 09:00 UTC never happened',
          },
          {
            name: 'web-2',
            current: 'running',
            desired: 'stopped',
            via_group: null,
            missed_at: null,
            changed_by_hand: true,
            reason: 'started by hand after the scheduled time',
          },
        ],
      },
    }),
  )
  let body: {
    names: string[]
  } | null = null
  await page.route('**/schedule/missed/apply', async (r: Route) => {
    body = r.request().postDataJSON()
    await r.fulfill({ json: { operation_id: 'op-c', total_steps: 1 } })
  })
  await page.route('**/operations/op-c', (r: Route) =>
    r.fulfill({
      json: {
        status: 'done',
        action: 'schedule_catch_up',
        percent: 100,
        current_step: null,
        warnings: [],
        result: { items: [{ name: 'web-1', action: 'start', outcome: 'started', ok: true, detail: null }] },
      },
    }),
  )
  await page.goto('/ui/#/instances')
  await expect(page.locator('main')).toContainText('2 nodes not in the state their schedule wants')
  await expect(page.getByLabel('Catch up web-1')).toBeChecked()
  await expect(page.getByLabel('Catch up web-2')).not.toBeChecked()
  await page.getByRole('button', { name: 'Apply schedule to 1 selected' }).click()
  expect(body).toBeNull()
  await page.getByRole('button', { name: 'Yes, apply' }).click()
  await expect.poll(() => body).not.toBeNull()
  expect(body).toEqual({ names: ['web-1'] })
  await expect(page.locator('main')).toContainText('web-1: start → started')
})
test('the system backup page shows key protection, problems and sends a test alert', async ({ page }) => {
  await loginAs(page)
  await mockInstanceList(page, {})
  await mockGroupList(page)
  await page.route('**/system/backup', (r: Route) =>
    r.fulfill({
      json: {
        object_storage: {
          configured: true,
          bucket: 'b',
          endpoint: 'https://e',
          access_key_hint: 'AK..',
          secret_key_set: true,
        },
        local_backup_dir: null,
        last_backup: null,
        continuous: {
          enabled: true,
          pending_changes: 0,
          last_backup_at: '2026-10-12T00:00:00Z',
          last_attempt_at: null,
          last_key: 'k',
          last_error: null,
          min_interval_seconds: 60,
        },
        ssh_key: {
          passphrase_set: true,
          stored_in_database: true,
          fingerprint: 'ab',
          in_object_storage: true,
          linode_ssh_key_id: 5,
          updated_at: null,
          passphrase_escrowed: false,
        },
        env_backup: { configured: false, recipients: [], uploaded_at: null, last_error: null },
      },
    }),
  )
  let recipientsBody: Record<string, unknown> | null = null
  await page.route('**/system/env-backup/recipients', async (r: Route) => {
    recipientsBody = r.request().postDataJSON()
    await r.fulfill({
      json: {
        status: { configured: true, recipients: ['age1abc...xyz'], uploaded_at: null, last_error: null },
        result: { outcome: 'uploaded', detail: null },
      },
    })
  })
  await page.route('**/system/health', (r: Route) =>
    r.fulfill({
      json: {
        problems: [
          {
            key: 'ssh-key',
            message:
              "the deployment SSH key isn't fully protected: no recovery copy of the passphrase in the Linode account",
          },
        ],
        alerts: { webhook: true, email: true, email_to: ['ops@example.com'] },
      },
    }),
  )
  let alertBody: Record<string, unknown> | null = null
  await page.route('**/system/alerts', async (r: Route) => {
    if (r.request().method() === 'PUT') {
      alertBody = r.request().postDataJSON()
      return r.fulfill({
        json: {
          webhook_set: true,
          webhook_host: 'hooks.slack.com',
          email_to: ['ops@example.com'],
          smtp_host: 'smtp.example.com',
          smtp_port: '587',
          smtp_security: 'starttls',
          smtp_user: 'u',
          smtp_password_set: true,
          email_from: null,
        },
      })
    }
    return r.fulfill({
      json: {
        webhook_set: true,
        webhook_host: 'hooks.slack.com',
        email_to: [],
        smtp_host: null,
        smtp_port: null,
        smtp_security: 'starttls',
        smtp_user: null,
        smtp_password_set: false,
        email_from: null,
      },
    })
  })
  let tested = 0
  await page.route('**/system/alerts/test', async (r: Route) => {
    tested += 1
    await r.fulfill({ json: { results: { webhook: true, email: false } } })
  })
  await page.goto('/ui/#/system-backup')
  await expect(page.locator('main')).toContainText('✗ Passphrase recovery copy in the Linode account')
  await expect(page.locator('main')).toContainText('no recovery copy of the passphrase')
  await expect(page.locator('main')).toContainText('Email: ops@example.com')
  await page.getByRole('button', { name: 'Send test alert' }).click()
  await expect(page.locator('main')).toContainText('Test alert failed on: email')
  expect(tested).toBe(1)
  await page.getByLabel('Send alerts to (comma-separated)').fill('ops@example.com')
  await page.getByLabel('SMTP server').fill('smtp.example.com')
  await page.getByLabel('Port').fill('587')
  await page.getByLabel('SMTP user').fill('u')
  await page.getByLabel('SMTP password').fill('app-password')
  await page.getByRole('button', { name: 'Save alert settings' }).click()
  await expect(page.locator('main')).toContainText('Alert settings saved')
  expect(alertBody).toMatchObject({
    email_to: 'ops@example.com',
    smtp_host: 'smtp.example.com',
    smtp_port: '587',
    smtp_user: 'u',
    smtp_password: 'app-password',
  })
  expect(alertBody).not.toHaveProperty('webhook_url')
  await expect(page.getByLabel('SMTP password')).toHaveValue('')
  await expect(page.locator('main')).toContainText('Encrypted settings backup (age)')
  await page
    .getByLabel('Age public key')
    .fill('age1qyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqsz5k3n7')
  await page.getByRole('button', { name: 'Save', exact: true }).click()
  await expect(page.locator('main')).toContainText('first encrypted copy uploaded')
  expect(recipientsBody).toEqual({
    recipients: 'age1qyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqsz5k3n7',
  })
})
test('a standby scheduler shows why and can take over after confirming; restore test runs', async ({
  page,
}) => {
  await loginAs(page)
  await mockInstanceList(page, {})
  await mockGroupList(page)
  await page.route('**/system/backup', (r: Route) =>
    r.fulfill({
      json: {
        object_storage: {
          configured: true,
          bucket: 'b',
          endpoint: 'https://e',
          access_key_hint: 'AK..',
          secret_key_set: true,
        },
        local_backup_dir: null,
        last_backup: null,
        continuous: null,
        ssh_key: null,
        env_backup: null,
        verify: {
          checked_at: '2026-10-12T00:00:00Z',
          ok: false,
          snapshot_key: 'k',
          problems: ['the newest snapshot fails its integrity check: x'],
        },
        lease: {
          role: 'standby',
          holder_host: 'old-host',
          holder_renewed_at: '2026-10-12T00:00:00Z',
          updated_at: '2026-10-12T00:00:00Z',
        },
      },
    }),
  )
  await page.route('**/system/health', (r: Route) =>
    r.fulfill({ json: { problems: [], alerts: { webhook: false, email: false, email_to: [] } } }),
  )
  await page.route('**/system/host-watchdog', (r: Route) =>
    r.fulfill({ json: { instance_id: 7, label: 'sched-host', watchdog_enabled: false } }),
  )
  let tookOver = 0
  await page.route('**/system/lease/take-over', async (r: Route) => {
    tookOver += 1
    await r.fulfill({ json: { active: true, role: 'active', holder: null, detail: null } })
  })
  await page.route('**/system/backup/verify', (r: Route) =>
    r.fulfill({ json: { ok: true, snapshot_key: 'k', problems: [], checks: ['a', 'b'] } }),
  )
  await page.goto('/ui/#/system-backup')
  await expect(page.locator('main')).toContainText('fails its integrity check')
  await expect(page.locator('main')).toContainText('old-host holds the lease')
  await expect(page.locator('main')).toContainText('Shutdown Watchdog on this host (sched-host)')
  await page.getByRole('button', { name: 'Take over as the active scheduler' }).click()
  expect(tookOver).toBe(0)
  await page.getByRole('button', { name: 'Yes, take over' }).click()
  await expect(page.locator('main')).toContainText('This host is now the active scheduler')
  await page.waitForTimeout(100)
  await page.getByRole('button', { name: 'Test restore now' }).click()
  await expect(page.locator('main')).toContainText('Restore test passed (2 checks)')
})
