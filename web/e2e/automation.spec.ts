import { expect, test } from '@playwright/test'
import type { Route } from '@playwright/test'
import { baseInstanceRecord, loginAs, mockGroupList, mockInstanceList } from './mocks'
test('creating an API token shows it once and lists it without the secret', async ({ page }) => {
  await loginAs(page)
  const tokens: Array<Record<string, unknown>> = []
  let createBody: Record<string, unknown> | null = null
  await page.route('**/tokens', async (route: Route) => {
    const req = route.request()
    if (req.method() === 'POST') {
      createBody = req.postDataJSON() as Record<string, unknown>
      const meta = {
        name: createBody.name,
        token_prefix: 'lis_abcdef',
        scopes: createBody.scopes,
        instances: null,
        groups: createBody.groups ?? null,
        created_by: 'alice',
        created_at: '2026-10-08T00:00:00+00:00',
        expires_at: null,
        revoked_at: null,
        last_used_at: null,
      }
      tokens.push(meta)
      await route.fulfill({ json: { ...meta, token: 'lis_secret-value-shown-once' } })
      return
    }
    await route.fulfill({ json: tokens })
  })
  await page.route('**/tokens/scopes', (route: Route) =>
    route.fulfill({
      json: {
        bundles: { read: ['instances:list'], operate: ['instances:start'], configure: [], admin: [] },
        operations: [
          { value: 'instances:start', description: 'start an instance' },
          { value: 'deletions:recover', description: 'recover a node or backup pending deletion' },
        ],
      },
    }),
  )
  await mockInstanceList(page, {
    'web-1': baseInstanceRecord(),
    'db-1': baseInstanceRecord({ schedule_mode: 'manual' }),
  })
  await mockGroupList(page, [
    { id: 1, name: 'dev', timezone: 'UTC', rules: [], enabled: true, member_count: 0 },
    { id: 2, name: 'prod', timezone: 'UTC', rules: [], enabled: true, member_count: 0 },
  ])
  await page.goto('/ui/#/tokens')
  await page.getByLabel('Token name').fill('ci-deploy')
  await page.getByRole('button', { name: 'Only these groups' }).click()
  await page.getByRole('option').getByText('dev', { exact: true }).click()
  await page.getByRole('button', { name: 'Done' }).click()
  await page.getByRole('button', { name: 'Only these nodes' }).click()
  await expect(page.getByRole('option').getByText('manual-only')).toBeVisible()
  await page.getByLabel('Search Only these nodes').fill('db')
  await expect(page.getByRole('option')).toHaveCount(1)
  await page.getByRole('option').getByText('db-1').click()
  await page.getByRole('button', { name: 'Done' }).click()
  await page.getByText('Or pick single operations').click()
  await expect(page.getByLabel('deletions:recover')).toBeVisible()
  await page.getByRole('button', { name: 'Create token' }).click()
  await expect(page.getByLabel('New token')).toHaveText('lis_secret-value-shown-once')
  expect(createBody).toEqual({
    name: 'ci-deploy',
    scopes: ['read', 'operate'],
    instances: ['db-1'],
    groups: ['dev'],
    expires_days: null,
  })
  await expect(page.getByText('ci-deploy', { exact: true })).toBeVisible()
  await expect(page.getByText('lis_abcdef…')).toBeVisible()
})
test('group "Start group" runs every member and shows the results', async ({ page }) => {
  await loginAs(page)
  await mockGroupList(page, [
    { id: 1, name: 'web', timezone: 'UTC', rules: [], enabled: true, member_count: 2 },
  ])
  await page.route('**/groups/web', async (route: Route) => {
    await route.fulfill({
      json: {
        id: 1,
        name: 'web',
        timezone: 'UTC',
        rules: [],
        enabled: true,
        members: ['web-1', 'web-2'],
        depends_on: [],
        dependents: [],
      },
    })
  })
  await page.route('**/groups/web/savings*', async (route: Route) => {
    await route.fulfill({
      json: {
        scheduled_savings_percent: null,
        actual_savings_percent: null,
        window_days: 7,
        schedule_state: 'none',
      },
    })
  })
  await page.route('**/groups/web/hooks', async (route: Route) => {
    await route.fulfill({ json: { hooks: null } })
  })
  await page.route('**/instances', async (route: Route) => {
    await route.fulfill({ json: {} })
  })
  let kickoffs = 0
  await page.route('**/groups/web/start', async (route: Route) => {
    kickoffs += 1
    await route.fulfill({ json: { operation_id: 'op-group-1', total_steps: 2 } })
  })
  await page.route('**/operations/op-group-1', async (route: Route) => {
    await route.fulfill({
      json: {
        status: 'done',
        action: 'group_start',
        percent: 100,
        current_step: null,
        warnings: [],
        operation_id: 'op-group-1',
        result: {
          group: 'web',
          action: 'start',
          stages: ['web'],
          ok: false,
          members: [
            {
              name: 'web-1',
              group: 'web',
              outcome: 'started',
              ok: true,
              detail: null,
              security_warning: false,
            },
            {
              name: 'web-2',
              group: 'web',
              outcome: 'create_failed',
              ok: false,
              detail: 'capacity',
              security_warning: false,
            },
          ],
        },
      },
    })
  })
  await page.goto('/ui/#/groups/web')
  await page.getByRole('button', { name: 'Start group' }).click()
  const results = page.getByLabel('Group action results')
  await expect(results).toContainText('web-1')
  await expect(results).toContainText('create failed')
  await expect(page.getByText('Some members did not start')).toBeVisible()
  expect(kickoffs).toBe(1)
})
