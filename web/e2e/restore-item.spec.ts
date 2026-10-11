import { expect, test } from '@playwright/test'
import type { Route } from '@playwright/test'
import { baseInstanceRecord, loginAs, mockGroupList, mockInstanceList } from './mocks'
async function setUp(page: import('@playwright/test').Page) {
  await loginAs(page)
  await mockInstanceList(page, { 'web-1': baseInstanceRecord(), 'db-1': baseInstanceRecord() })
  await mockGroupList(page, [
    { id: 1, name: 'dev', timezone: 'UTC', rules: [], enabled: true, member_count: 1 },
  ])
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
        verify: null,
        lease: null,
      },
    }),
  )
  await page.route('**/system/health', (r: Route) =>
    r.fulfill({ json: { problems: [], alerts: { webhook: false, email: false, email_to: [] } } }),
  )
  await page.route('**/system/host-watchdog', (r: Route) =>
    r.fulfill({ json: { instance_id: 7, label: 'sched-host', watchdog_enabled: true } }),
  )
  await page.route('**/system/snapshots', (r: Route) =>
    r.fulfill({
      json: { snapshots: ['full-backups/instances-1.db', 'full-backups/instances-continuous.db'] },
    }),
  )
  const bodies: Array<Record<string, unknown>> = []
  await page.route('**/system/restore-item/preview', async (r: Route) => {
    bodies.push(r.request().postDataJSON())
    await r.fulfill({ json: { snapshot: { enabled: true }, current: { enabled: false } } })
  })
  return bodies
}
test('picks the instance from a dropdown', async ({ page }) => {
  const bodies = await setUp(page)
  await page.goto('/ui/#/system-backup')
  const card = page.getByTestId('restore-item-card')
  await card.getByRole('button', { name: 'Instance', exact: true }).click()
  await card.getByLabel('Search Instance').fill('db')
  const options = card.getByRole('listbox').getByRole('option')
  await expect(options.filter({ hasText: 'db-1' })).toBeVisible()
  await expect(options.filter({ hasText: 'web-1' })).toHaveCount(0)
  await card.getByRole('listbox').getByRole('option').filter({ hasText: 'db-1' }).locator('input').click()
  await card.getByRole('button', { name: 'Show what the snapshot has' }).click()
  await expect.poll(() => bodies.length).toBe(1)
  expect(bodies[0]).toMatchObject({ kind: 'schedule', name: 'db-1' })
})
test('a group that no longer exists can still be typed and restored', async ({ page }) => {
  const bodies = await setUp(page)
  await page.goto('/ui/#/system-backup')
  const card = page.getByTestId('restore-item-card')
  await card.locator('select').first().selectOption('group')
  await card.getByRole('button', { name: 'Group', exact: true }).click()
  await expect(card.getByRole('listbox').getByRole('option').filter({ hasText: 'dev' })).toBeVisible()
  await card.getByLabel('Search Group').fill('old-team')
  await card.getByRole('button', { name: /Use “old-team”/ }).click()
  await card.getByRole('button', { name: 'Show what the snapshot has' }).click()
  await expect.poll(() => bodies.length).toBe(1)
  expect(bodies[0]).toMatchObject({ kind: 'group', name: 'old-team' })
})
