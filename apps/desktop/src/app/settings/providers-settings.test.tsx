import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { atom } from 'nanostores'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { bindConfigReadOrigin } from '@/api/config'
import { ConfirmHost } from '@/components/confirm-host'
import { $confirmRequest } from '@/store/confirm'
import type { EnvVarInfo, OAuthProvider } from '@/types/hermes'

const listOAuthProviders = vi.fn()
const disconnectOAuthProvider = vi.fn()
const updateOAuthAccount = vi.fn()
const deleteOAuthAccount = vi.fn()
const linkOAuthAccount = vi.fn()
const getEnvVars = vi.fn()
const revealEnvVar = vi.fn()
const setEnvVar = vi.fn()
const startManualProviderOAuth = vi.fn()
const startManualLocalEndpoint = vi.fn()
const onboarding = atom({ manual: false })

vi.mock('@/store/profile', () => ({
  $activeGatewayProfile: atom('alpha'),
  $profiles: atom([]),
  refreshProfiles: async () => {},
  normalizeProfileKey: (p: string | null) => p || 'default',
  profileLabel: (p: { display_name?: string; name: string }) => p.display_name || p.name
}))

vi.mock('@/hermes', () => ({
  setApiRequestProfile: vi.fn(),
  getProfiles: async () => ({ profiles: (await import('@/store/profile')).$profiles.get() }),
  disconnectOAuthProvider: (...args: unknown[]) => disconnectOAuthProvider(...args),
  updateOAuthAccount: (...args: unknown[]) => updateOAuthAccount(...args),
  deleteOAuthAccount: (...args: unknown[]) => deleteOAuthAccount(...args),
  linkOAuthAccount: (...args: unknown[]) => linkOAuthAccount(...args),
  getEnvVars: (...args: unknown[]) => getEnvVars(...args),
  listOAuthProviders: (...args: unknown[]) => listOAuthProviders(...args),
  revealEnvVar: (key: string, profile?: string) => revealEnvVar(key, profile),
  setEnvVar: (key: string, value: string, profile?: string) => setEnvVar(key, value, profile)
}))

vi.mock('@/store/onboarding', () => ({
  $desktopOnboarding: onboarding,
  startManualProviderOAuth: (...args: unknown[]) => startManualProviderOAuth(...args),
  startManualLocalEndpoint: (reason: null | string) => startManualLocalEndpoint(reason)
}))

// Load once at module scope so no test's 15s budget pays the heavy transform
// + import (the first-test timeout flake under CI load).
const { ProvidersSettings } = await import('./providers-settings')
const { $settingsScopeOverride } = await import('@/store/settings-scope')
const { $activeGatewayProfile, $profiles } = await import('@/store/profile')

function provider(id: string, loggedIn: boolean, patch: Partial<OAuthProvider> = {}): OAuthProvider {
  return {
    cli_command: `hermes auth add ${id}`,
    disconnectable: true,
    docs_url: '',
    flow: 'device_code',
    id,
    name: id === 'nous' ? 'Nous Portal' : 'MiniMax',
    status: {
      logged_in: loggedIn
    },
    ...patch
  }
}

// One `/api/env` row (an EnvVarInfo) for the API-keys view. Mirrors the
// `provider()` factory above: a valid base + per-test overrides, typed against
// the real response shape so it can't drift from EnvVarInfo.
function keyVar(patch: Partial<EnvVarInfo> = {}): EnvVarInfo {
  return {
    advanced: false,
    category: 'provider',
    description: '',
    is_password: true,
    is_set: false,
    provider: '',
    provider_label: '',
    redacted_value: null,
    tools: [],
    url: '',
    ...patch
  }
}

beforeEach(() => {
  onboarding.set({ manual: false })
  getEnvVars.mockResolvedValue({})
  disconnectOAuthProvider.mockResolvedValue({ ok: true, provider: 'nous' })
  updateOAuthAccount.mockResolvedValue({ ok: true })
  deleteOAuthAccount.mockResolvedValue({ ok: true })
  linkOAuthAccount.mockResolvedValue({ ok: true })
  revealEnvVar.mockResolvedValue({ value: 'old-secret' })
  setEnvVar.mockResolvedValue({ ok: true })
  listOAuthProviders.mockResolvedValue({
    providers: [provider('nous', true), provider('minimax-oauth', false)]
  })
})

afterEach(() => {
  cleanup()
  $confirmRequest.set(null)
  vi.restoreAllMocks()
  vi.clearAllMocks()
})

// Removal goes through confirm() from @/store/confirm, so the host has to be
// mounted for the prompt to render — same as in the real app shell.
async function renderProvidersSettings() {
  let result: ReturnType<typeof render>
  await act(async () => {
    result = render(
      <>
        <ProvidersSettings onClose={vi.fn()} onViewChange={vi.fn()} view="accounts" />
        <ConfirmHost />
      </>
    )
  })

  return result!
}

describe('ProvidersSettings', () => {
  function managedProvider(patch: Partial<OAuthProvider> = {}) {
    const value = provider('openai-codex', true, {
      name: 'OpenAI Codex',
      supports_add_account: true,
      supports_account_management: true,
      accounts: [
        { id: 'stable-first', label: 'Work', priority: 0, enabled: true, last_status: null },
        { id: 'stable-second', label: 'Personal', priority: 1, enabled: false, last_status: 'exhausted' }
      ],
      ...patch
    })

    bindConfigReadOrigin(value, { connectionId: 'connection-a', profile: 'beta' })

    return value
  }

  it('uses per-account removal instead of the legacy provider disconnect for managed accounts', async () => {
    listOAuthProviders.mockResolvedValue({ providers: [managedProvider()] })
    await renderProvidersSettings()
    expect(screen.queryByRole('button', { name: 'Remove ChatGPT or Codex Subscription' })).toBeNull()
    expect(
      within(screen.getByRole('listitem', { name: 'Work' })).getByRole('button', { name: 'Delete account' })
    ).toBeTruthy()
  })

  it('toggles an account by stable id in the captured scope and waits for server truth', async () => {
    const value = managedProvider()
    let completeRefresh!: (response: { providers: OAuthProvider[] }) => void
    listOAuthProviders.mockResolvedValueOnce({ providers: [value] }).mockImplementationOnce(
      () =>
        new Promise(resolve => {
          completeRefresh = resolve
        })
    )
    await renderProvidersSettings()
    const row = screen.getByRole('listitem', { name: 'Personal' })
    fireEvent.click(within(row).getByRole('switch', { name: 'Use in this profile' }))
    await waitFor(() =>
      expect(updateOAuthAccount).toHaveBeenCalledWith(
        'openai-codex',
        'stable-second',
        { enabled: true },
        { connectionId: 'connection-a', profile: 'beta' }
      )
    )
    expect(within(row).getByRole('switch').getAttribute('aria-checked')).toBe('false')
    expect((within(row).getByRole('switch') as HTMLButtonElement).disabled).toBe(true)
    expect(listOAuthProviders).toHaveBeenLastCalledWith({ connectionId: 'connection-a', profile: 'beta' })
    await act(async () =>
      completeRefresh({
        providers: [
          managedProvider({
            accounts: [{ id: 'stable-second', label: 'Personal', priority: 0, enabled: true, last_status: 'exhausted' }]
          })
        ]
      })
    )
    expect(screen.getByRole('switch').getAttribute('aria-checked')).toBe('true')
    expect(screen.getByText('Exhausted')).toBeTruthy()
  })

  it('renames and reorders individual accounts, then reloads the list', async () => {
    const value = managedProvider()
    listOAuthProviders.mockResolvedValue({ providers: [value] })
    await renderProvidersSettings()
    const row = screen.getByRole('listitem', { name: 'Personal' })
    fireEvent.click(within(row).getByRole('button', { name: 'Rename account' }))
    fireEvent.change(within(row).getByRole('textbox', { name: 'Account name' }), { target: { value: 'Home' } })
    fireEvent.click(within(row).getByRole('button', { name: 'Save' }))
    await waitFor(() =>
      expect(updateOAuthAccount).toHaveBeenCalledWith(
        'openai-codex',
        'stable-second',
        { label: 'Home' },
        { connectionId: 'connection-a', profile: 'beta' }
      )
    )
    await waitFor(() => expect(within(row).queryByRole('textbox')).toBeNull())
    fireEvent.click(within(row).getByRole('button', { name: 'Move up' }))
    await waitFor(() =>
      expect(updateOAuthAccount).toHaveBeenCalledWith(
        'openai-codex',
        'stable-second',
        { priority: 0 },
        { connectionId: 'connection-a', profile: 'beta' }
      )
    )
    expect(listOAuthProviders).toHaveBeenCalledTimes(3)
  })

  it.each([
    { shared: false, label: 'Delete account', title: 'Delete Work?' },
    { shared: true, label: 'Unlink from profile', title: 'Unlink Work from this profile?' }
  ])('confirms $label and removes only the selected stable account id', async ({ shared, label, title }) => {
    listOAuthProviders.mockResolvedValue({
      providers: [
        managedProvider({
          accounts: [
            {
              id: 'stable-first',
              label: 'Work',
              priority: 0,
              enabled: true,
              owner_profile: shared ? 'team' : 'beta',
              shared
            }
          ]
        })
      ]
    })
    await renderProvidersSettings()
    const row = screen.getByRole('listitem', { name: 'Work' })

    if (shared) {
      expect(within(row).getByText('From profile: team')).toBeTruthy()
    }

    fireEvent.click(within(row).getByRole('button', { name: label }))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText(title)).toBeTruthy()
    expect(deleteOAuthAccount).not.toHaveBeenCalled()
    fireEvent.click(within(dialog).getByRole('button', { name: label }))
    await waitFor(() =>
      expect(deleteOAuthAccount).toHaveBeenCalledWith('openai-codex', 'stable-first', {
        connectionId: 'connection-a',
        profile: 'beta'
      })
    )
    expect(disconnectOAuthProvider).not.toHaveBeenCalled()
    expect(listOAuthProviders).toHaveBeenCalledTimes(2)
  })

  it('attaches an existing account from another profile without starting OAuth', async () => {
    // Radix Select scrolls the focused option; jsdom has no layout API.
    Object.defineProperty(Element.prototype, 'scrollIntoView', { configurable: true, writable: true, value: vi.fn() })
    listOAuthProviders.mockResolvedValue({
      providers: [
        managedProvider({
          status: { logged_in: false },
          accounts: [],
          available_accounts: [{ id: 'shared-id', label: 'Work', owner_profile: 'team', priority: 0, enabled: true }]
        })
      ]
    })
    await renderProvidersSettings()
    fireEvent.click(screen.getByRole('button', { name: 'Other providers' }))
    fireEvent.click(screen.getByRole('combobox', { name: 'Existing account' }))
    fireEvent.click(await screen.findByRole('option', { name: 'Work · team' }))
    fireEvent.click(screen.getByRole('button', { name: 'Link account' }))
    await waitFor(() =>
      expect(linkOAuthAccount).toHaveBeenCalledWith('openai-codex', 'team', 'shared-id', {
        connectionId: 'connection-a',
        profile: 'beta'
      })
    )
    expect(startManualProviderOAuth).not.toHaveBeenCalled()
    expect(listOAuthProviders).toHaveBeenCalledTimes(2)
  })

  it('keeps failed writes visible and leaves the server account state intact', async () => {
    listOAuthProviders.mockResolvedValue({ providers: [managedProvider()] })
    updateOAuthAccount.mockRejectedValueOnce(new Error('Account is busy'))
    await renderProvidersSettings()
    const row = screen.getByRole('listitem', { name: 'Work' })
    fireEvent.click(within(row).getByRole('switch'))
    expect((await within(row).findByRole('alert')).textContent).toContain('Account is busy')
    expect(within(row).getByRole('switch').getAttribute('aria-checked')).toBe('true')
    expect((within(row).getByRole('switch') as HTMLButtonElement).disabled).toBe(false)
    expect(listOAuthProviders).toHaveBeenCalledTimes(1)
  })

  it('refreshes both writes when two account rows are changed before the first reload completes', async () => {
    let completeSecondWrite!: (result: { ok: boolean }) => void
    updateOAuthAccount.mockResolvedValueOnce({ ok: true }).mockImplementationOnce(
      () =>
        new Promise(resolve => {
          completeSecondWrite = resolve
        })
    )
    listOAuthProviders
      .mockResolvedValueOnce({ providers: [managedProvider()] })
      .mockResolvedValueOnce({
        providers: [
          managedProvider({
            accounts: [
              { id: 'stable-first', label: 'Work', priority: 0, enabled: false },
              { id: 'stable-second', label: 'Personal', priority: 1, enabled: false }
            ]
          })
        ]
      })
      .mockResolvedValueOnce({
        providers: [
          managedProvider({
            accounts: [
              { id: 'stable-first', label: 'Work', priority: 0, enabled: false },
              { id: 'stable-second', label: 'Personal', priority: 1, enabled: true }
            ]
          })
        ]
      })
    await renderProvidersSettings()
    fireEvent.click(within(screen.getByRole('listitem', { name: 'Work' })).getByRole('switch'))
    fireEvent.click(within(screen.getByRole('listitem', { name: 'Personal' })).getByRole('switch'))
    await waitFor(() => expect(listOAuthProviders).toHaveBeenCalledTimes(2))
    await act(async () => completeSecondWrite({ ok: true }))
    expect(
      within(screen.getByRole('listitem', { name: 'Personal' }))
        .getByRole('switch')
        .getAttribute('aria-checked')
    ).toBe('true')
  })

  it('shows a disabled or missing source separately from the profile assignment toggle', async () => {
    listOAuthProviders.mockResolvedValue({
      providers: [
        managedProvider({
          accounts: [
            {
              id: 'link-disabled',
              label: 'Shared',
              priority: 0,
              enabled: false,
              configured_enabled: true,
              owner_enabled: false,
              owner_profile: 'team',
              owner_credential_id: 'canonical',
              shared: true
            },
            {
              id: 'link-missing',
              label: 'Missing',
              priority: 1,
              enabled: false,
              configured_enabled: true,
              owner_profile: 'former',
              shared: true,
              missing: true,
              unavailable_reason: 'owner_missing'
            }
          ]
        })
      ]
    })
    await renderProvidersSettings()
    const disabled = within(screen.getByRole('listitem', { name: 'Shared' }))
    expect(disabled.getByText('Disabled')).toBeTruthy()
    expect(disabled.getByText('The source account is disabled in its owner profile.')).toBeTruthy()
    expect(disabled.getByRole('switch').getAttribute('aria-checked')).toBe('true')
    const missing = within(screen.getByRole('listitem', { name: 'Missing' }))
    expect(missing.getByText('Unavailable')).toBeTruthy()
    expect(missing.getByText('The source account is no longer available.')).toBeTruthy()
    expect(missing.getByRole('button', { name: 'Unlink from profile' })).toBeTruthy()
  })

  it('does not offer accounts whose canonical owner identity is already linked', async () => {
    Object.defineProperty(Element.prototype, 'scrollIntoView', { configurable: true, writable: true, value: vi.fn() })
    listOAuthProviders.mockResolvedValue({
      providers: [
        managedProvider({
          accounts: [
            {
              id: 'local-link',
              label: 'Shared',
              priority: 0,
              enabled: true,
              owner_profile: 'team',
              owner_credential_id: 'canonical',
              shared: true
            }
          ],
          available_accounts: [
            { id: 'canonical', label: 'Already linked', priority: 0, enabled: true, owner_profile: 'team' },
            { id: 'canonical', label: 'Other owner', priority: 0, enabled: true, owner_profile: 'other' }
          ]
        })
      ]
    })
    await renderProvidersSettings()
    fireEvent.click(screen.getByRole('combobox', { name: 'Existing account' }))
    expect(await screen.findByRole('option', { name: 'Other owner · other' })).toBeTruthy()
    expect(screen.queryByRole('option', { name: 'Already linked · team' })).toBeNull()
  })

  it('does not repaint an old profile when its account refresh finishes after a scope switch', async () => {
    $settingsScopeOverride.set('beta')
    let completeRefresh!: (response: { providers: OAuthProvider[] }) => void
    const gamma = managedProvider({ accounts: [{ id: 'gamma-account', label: 'Gamma', priority: 0, enabled: true }] })
    bindConfigReadOrigin(gamma, { connectionId: 'connection-a', profile: 'gamma' })
    listOAuthProviders
      .mockResolvedValueOnce({ providers: [managedProvider()] })
      .mockImplementationOnce(
        () =>
          new Promise(resolve => {
            completeRefresh = resolve
          })
      )
      .mockResolvedValueOnce({ providers: [gamma] })

    try {
      await renderProvidersSettings()
      fireEvent.click(within(screen.getByRole('listitem', { name: 'Work' })).getByRole('switch'))
      await waitFor(() => expect(listOAuthProviders).toHaveBeenCalledTimes(2))
      await act(async () => $settingsScopeOverride.set('gamma'))
      expect(await screen.findByText('Gamma')).toBeTruthy()
      await act(async () => completeRefresh({ providers: [managedProvider()] }))
      expect(screen.getByText('Gamma')).toBeTruthy()
      expect(screen.queryByRole('listitem', { name: 'Work' })).toBeNull()
    } finally {
      await act(async () => $settingsScopeOverride.set(null))
    }
  })

  it('keeps an additive OAuth sign-in on the connection that served the account list', async () => {
    listOAuthProviders.mockResolvedValue({ providers: [managedProvider()] })
    await renderProvidersSettings()
    fireEvent.click(screen.getByRole('button', { name: 'Add account' }))
    expect(startManualProviderOAuth).toHaveBeenCalledWith(
      'openai-codex',
      { connectionId: 'connection-a', profile: 'beta' },
      true
    )
  })

  it('shows separate Codex accounts and starts an additive login in the settings profile', async () => {
    $settingsScopeOverride.set('beta')
    listOAuthProviders.mockResolvedValue({
      providers: [
        provider('openai-codex', true, {
          name: 'OpenAI Codex',
          supports_add_account: true,
          accounts: [
            { id: 'first', label: 'first@example.com', priority: 0 },
            { id: 'second', label: 'second@example.com', priority: 1 }
          ]
        })
      ]
    })

    try {
      await renderProvidersSettings()
      expect(await screen.findByText('first@example.com')).toBeTruthy()
      expect(screen.getByText('second@example.com')).toBeTruthy()
      fireEvent.click(screen.getByRole('button', { name: 'Add account' }))
      expect(startManualProviderOAuth).toHaveBeenCalledWith('openai-codex', 'beta', true)
      expect(disconnectOAuthProvider).not.toHaveBeenCalled()
    } finally {
      $settingsScopeOverride.set(null)
    }
  })

  it('keeps Accounts visible when the provider request fails', async () => {
    listOAuthProviders.mockRejectedValue(new Error('Provider service unavailable'))
    getEnvVars.mockResolvedValue({ WIDGET_API_KEY: keyVar({ provider: 'widget', provider_label: 'Widget' }) })
    await renderProvidersSettings()
    expect((await screen.findByRole('alert')).textContent).toContain('Provider service unavailable')
    expect(screen.queryByText('Widget')).toBeNull()
    listOAuthProviders.mockResolvedValue({ providers: [provider('nous', true)] })
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(await screen.findByText('Nous Portal')).toBeTruthy()
  })

  it('reads and saves API keys for the shared Settings target and reloads when it changes', async () => {
    $activeGatewayProfile.set('profile-a')
    $settingsScopeOverride.set('profile-b')
    $profiles.set(
      ['profile-a', 'profile-b'].map(name => ({
        name,
        has_env: false,
        is_default: false,
        model: null,
        path: '',
        provider: null,
        skill_count: 0
      }))
    )
    getEnvVars.mockResolvedValue({ WIDGET_API_KEY: keyVar({ provider: 'widget', provider_label: 'Widget' }) })

    try {
      const { container } = render(<ProvidersSettings onClose={vi.fn()} onViewChange={vi.fn()} view="keys" />)
      await screen.findByText('Widget')
      expect(getEnvVars).toHaveBeenLastCalledWith('profile-b')
      expect(screen.getByText('Applies to')).toBeTruthy()
      const input = container.querySelector('input[type="password"]')!
      fireEvent.focus(input)
      fireEvent.change(input, { target: { value: 'fixture-key' } })
      fireEvent.click(screen.getByRole('button', { name: 'Save' }))
      await waitFor(() => expect(setEnvVar).toHaveBeenCalledWith('WIDGET_API_KEY', 'fixture-key', 'profile-b'))
      fireEvent.click(screen.getByRole('button', { name: 'profile-a' }))
      // Back onto the app's active profile: no override is stored, but the
      // request must still name it (#118432).
      await waitFor(() => expect(getEnvVars).toHaveBeenLastCalledWith('profile-a'))
    } finally {
      cleanup()
      $settingsScopeOverride.set(null)
      $activeGatewayProfile.set('default')
      $profiles.set([])
    }
  })

  it('uses the settings target for account reads, removal and sign-in', async () => {
    $settingsScopeOverride.set('beta')

    try {
      await renderProvidersSettings()
      expect(getEnvVars).toHaveBeenCalledWith('beta')
      expect(listOAuthProviders).toHaveBeenCalledWith('beta')
      fireEvent.click(await screen.findByText('Nous Portal'))
      expect(startManualProviderOAuth).toHaveBeenCalledWith('nous', 'beta')
      fireEvent.click(await screen.findByRole('button', { name: 'Remove Nous Portal' }))
      fireEvent.click(await screen.findByRole('button', { name: 'Disconnect' }))
      await waitFor(() => expect(disconnectOAuthProvider).toHaveBeenCalledWith('nous', 'beta'))
    } finally {
      $settingsScopeOverride.set(null)
    }
  })

  it('disconnects a connected provider account and refreshes the accounts list', async () => {
    await renderProvidersSettings()

    const remove = await screen.findByRole('button', { name: 'Remove Nous Portal' })
    await act(async () => {
      fireEvent.click(remove)
    })

    // Removal is confirmed first — nothing has been disconnected yet.
    expect(await screen.findByRole('dialog')).toBeTruthy()
    expect(disconnectOAuthProvider).not.toHaveBeenCalled()

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Disconnect' }))
    })

    await waitFor(() => expect(disconnectOAuthProvider).toHaveBeenCalledWith('nous', 'default'))
    expect(listOAuthProviders).toHaveBeenCalledTimes(2)
  })

  it('leaves the account connected when the removal prompt is dismissed', async () => {
    await renderProvidersSettings()

    await act(async () => {
      fireEvent.click(await screen.findByRole('button', { name: 'Remove Nous Portal' }))
    })

    await act(async () => {
      fireEvent.click(await screen.findByRole('button', { name: 'Cancel' }))
    })

    expect(disconnectOAuthProvider).not.toHaveBeenCalled()
  })

  it('does not offer removal for externally managed providers', async () => {
    listOAuthProviders.mockResolvedValue({
      providers: [
        provider('qwen-oauth', true, {
          cli_command: 'hermes auth add qwen-oauth',
          disconnect_hint: "Use `hermes auth add qwen-oauth` or that provider's CLI to remove it.",
          disconnectable: false,
          flow: 'external',
          name: 'Qwen (via Qwen CLI)'
        })
      ]
    })

    await renderProvidersSettings()

    expect(await screen.findByText('Qwen Code')).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Remove Qwen Code' })).toBeNull()
  })

  it('renders a Keys card for a backend-tagged provider with no PROVIDER_GROUPS prefix', async () => {
    // A provider the backend catalog tags (provider/provider_label) but that has
    // no desktop PROVIDER_GROUPS prefix row must still render its own card —
    // this is the GUI/CLI drift fix: membership comes from the backend, not
    // from the hand-maintained prefix list.
    getEnvVars.mockResolvedValue({
      WIDGETAI_API_KEY: keyVar({
        provider: 'widgetai',
        provider_label: 'WidgetAI',
        url: 'https://widgetai.example/keys'
      })
    })
    listOAuthProviders.mockResolvedValue({ providers: [] })

    await act(async () => {
      render(<ProvidersSettings onClose={vi.fn()} onViewChange={vi.fn()} view="keys" />)
    })

    expect(await screen.findByText('WidgetAI')).toBeTruthy()
  })

  it('renders separate provider cards that share one credential env var', async () => {
    getEnvVars.mockResolvedValue({
      DASHSCOPE_API_KEY: keyVar({
        provider: 'alibaba',
        provider_label: 'Qwen Cloud',
        provider_profiles: [
          {
            description: 'International DashScope route',
            primary: true,
            provider: 'alibaba',
            provider_label: 'Qwen Cloud',
            url: 'https://modelstudio.console.alibabacloud.com/'
          },
          {
            description: 'Mainland-China DashScope route',
            primary: true,
            provider: 'alibaba-cn',
            provider_label: 'Alibaba Cloud DashScope (China)',
            url: 'https://bailian.console.aliyun.com/'
          }
        ]
      })
    })
    listOAuthProviders.mockResolvedValue({ providers: [] })

    const { ProvidersSettings } = await import('./providers-settings')
    render(<ProvidersSettings onClose={vi.fn()} onViewChange={vi.fn()} view="keys" />)

    expect(await screen.findByText('Qwen Cloud')).toBeTruthy()
    expect(screen.getByText('Alibaba Cloud DashScope (China)')).toBeTruthy()
    const inputs = screen.getAllByPlaceholderText(/Paste .* key/)
    expect(inputs).toHaveLength(2)

    fireEvent.focus(inputs[0])
    fireEvent.change(inputs[0], { target: { value: 'shared-secret' } })

    expect(screen.getAllByDisplayValue('shared-secret')).toHaveLength(1)
    expect((inputs[1] as HTMLInputElement).value).toBe('')
  })

  it('keeps a card on its own key when a shared credential arrives with primary: false', async () => {
    // The CN Coding Plan card's own credential (its index-0 var) plus the
    // shared DASHSCOPE_API_KEY, which the catalog contributes as a FALLBACK
    // alias (primary: false) because it is index >= 1 for that provider. The
    // card's "Paste key" must edit the provider's own key, not the shared one.
    getEnvVars.mockResolvedValue({
      ALIBABA_CODING_PLAN_CN_API_KEY: keyVar({
        provider: 'alibaba-coding-plan-cn',
        provider_label: 'Alibaba Cloud (Coding Plan, China)',
        provider_primary: true
      }),
      ALIBABA_CODING_PLAN_API_KEY: keyVar({
        provider: 'alibaba-coding-plan-cn',
        provider_label: 'Alibaba Cloud (Coding Plan, China)',
        provider_primary: false
      }),
      DASHSCOPE_API_KEY: keyVar({
        provider: 'alibaba',
        provider_label: 'Qwen Cloud',
        provider_profiles: [
          {
            description: 'International DashScope route',
            primary: true,
            provider: 'alibaba',
            provider_label: 'Qwen Cloud',
            url: 'https://modelstudio.console.alibabacloud.com/'
          },
          {
            description: 'Coding Plan fallback alias',
            primary: false,
            provider: 'alibaba-coding-plan-cn',
            provider_label: 'Alibaba Cloud (Coding Plan, China)',
            url: 'https://help.aliyun.com/zh/model-studio/'
          }
        ]
      })
    })
    listOAuthProviders.mockResolvedValue({ providers: [] })

    const { ProvidersSettings } = await import('./providers-settings')
    const { container } = render(<ProvidersSettings onClose={vi.fn()} onViewChange={vi.fn()} view="keys" />)

    expect(await screen.findByText('Alibaba Cloud (Coding Plan, China)')).toBeTruthy()

    // Exactly one primary "Paste … key" input per card; the CN card's must edit
    // ALIBABA_CODING_PLAN_CN_API_KEY, never the shared DASHSCOPE_API_KEY.
    const inputs = container.querySelectorAll('input[type="password"]')
    const pasteInputs = await screen.findAllByPlaceholderText(/Paste .* key/)
    expect(pasteInputs).toHaveLength(2) // Qwen Cloud card + the CN Coding Plan card

    const cnCard = screen
      .getAllByText('Alibaba Cloud (Coding Plan, China)')
      .map(el => el.closest('[role="button"]') ?? el.closest('div[class*="group/card"]'))
      .find(Boolean)!

    const cnInput = cnCard.querySelector('input[type="password"]')!
    expect(inputs.length).toBeGreaterThanOrEqual(1)

    fireEvent.focus(cnInput)
    fireEvent.change(cnInput, { target: { value: 'cn-tier-secret' } })
    fireEvent.click(within(cnCard as HTMLElement).getByRole('button', { name: 'Save' }))

    // The write names the CN-specific var — never the shared DASHSCOPE_API_KEY.
    await waitFor(() => {
      const [key, value] = setEnvVar.mock.calls.at(-1) ?? []
      expect(key).toBe('ALIBABA_CODING_PLAN_CN_API_KEY')
      expect(value).toBe('cn-tier-secret')
      expect(setEnvVar).not.toHaveBeenCalledWith('DASHSCOPE_API_KEY', expect.anything(), expect.anything())
    })
  })

  it('clears the shared reveal when a namespaced provider-card draft is saved', async () => {
    const varKey = 'DASHSCOPE_API_KEY'
    const editKey = `Qwen Cloud:${varKey}`
    getEnvVars.mockResolvedValue({
      [varKey]: keyVar({ is_set: true, redacted_value: '••••••••' })
    })

    const { useEnvCredentials } = await import('./env-credentials')
    const state = { current: null as null | ReturnType<typeof useEnvCredentials> }

    function Harness() {
      state.current = useEnvCredentials()

      return null
    }

    render(<Harness />)
    await waitFor(() => expect(state.current?.vars).not.toBeNull())

    await act(async () => {
      await Promise.resolve(state.current!.rowProps.onReveal(varKey))
    })
    expect(state.current!.rowProps.revealed[varKey]).toBe('old-secret')

    act(() => {
      state.current!.rowProps.setEdits(current => ({ ...current, [editKey]: 'new-secret' }))
    })
    await waitFor(() => expect(state.current!.rowProps.edits[editKey]).toBe('new-secret'))

    await act(async () => {
      await Promise.resolve(state.current!.rowProps.onSave(varKey, editKey))
    })

    expect(setEnvVar).toHaveBeenCalledWith(varKey, 'new-secret', undefined)
    expect(state.current!.rowProps.edits[editKey]).toBeUndefined()
    expect(state.current!.rowProps.revealed[varKey]).toBeUndefined()
  })

  it('orders API-key providers by priority then name, and filters them via search', async () => {
    // These three providers have no curated PROVIDER_GROUPS priority, so they
    // share the default priority and fall back to alphabetical among themselves
    // (Acme, Middle, Zebra) — exercising the name tiebreak of the priority sort.
    getEnvVars.mockResolvedValue({
      ZEBRA_API_KEY: keyVar({ provider: 'zebra', provider_label: 'Zebra' }),
      ACME_API_KEY: keyVar({ provider: 'acme', provider_label: 'Acme' }),
      MIDDLE_API_KEY: keyVar({ provider: 'middle', provider_label: 'Middle' })
    })
    listOAuthProviders.mockResolvedValue({ providers: [] })

    render(<ProvidersSettings onClose={vi.fn()} onViewChange={vi.fn()} view="keys" />)

    // Equal priority → alphabetical tiebreak: Acme, Middle, Zebra.
    await screen.findByText('Acme')
    const labels = screen.getAllByText(/Acme|Middle|Zebra/).map(el => el.textContent)
    expect(labels).toEqual(['Acme', 'Middle', 'Zebra'])

    // Typing narrows the list to matching providers only.
    const search = screen.getByPlaceholderText('Search providers…')
    await act(async () => {
      fireEvent.change(search, { target: { value: 'mid' } })
    })

    await waitFor(() => expect(screen.queryByText('Acme')).toBeNull())
    expect(screen.getByText('Middle')).toBeTruthy()
    expect(screen.queryByText('Zebra')).toBeNull()

    // A non-matching query shows the empty-state copy.
    await act(async () => {
      fireEvent.change(search, { target: { value: 'nonesuch-xyz' } })
    })
    expect(await screen.findByText('No providers match your search.')).toBeTruthy()
  })

  it('expands and scrolls to the provider named by a ?key= deep link', async () => {
    Element.prototype.scrollIntoView = vi.fn()
    getEnvVars.mockResolvedValue({
      ACME_API_KEY: keyVar({ description: 'Acme blurb', provider: 'acme', provider_label: 'Acme' }),
      ZEBRA_API_KEY: keyVar({ description: 'Zebra blurb', provider: 'zebra', provider_label: 'Zebra' })
    })
    listOAuthProviders.mockResolvedValue({ providers: [] })

    render(
      <MemoryRouter initialEntries={['/settings?tab=providers&pview=keys&key=ZEBRA_API_KEY']}>
        <ProvidersSettings onClose={vi.fn()} onViewChange={vi.fn()} view="keys" />
      </MemoryRouter>
    )

    expect(await screen.findByText('Zebra blurb')).toBeTruthy()
    expect(screen.queryByText('Acme blurb')).toBeNull()
    await waitFor(() => expect(Element.prototype.scrollIntoView).toHaveBeenCalled())
  })

  it('offers a Local / custom endpoint entry in the API-keys tab that opens the custom-endpoint flow', async () => {
    // Regression: the composer pill and the providers "have an API key"
    // affordance both dead-end on the env-var-driven key catalog, which never
    // lists a custom endpoint — so without this row there is no reachable
    // Desktop GUI path to add one. See issue #62817.
    getEnvVars.mockResolvedValue({})
    listOAuthProviders.mockResolvedValue({ providers: [] })

    render(<ProvidersSettings onClose={vi.fn()} onViewChange={vi.fn()} view="keys" />)

    const row = await screen.findByText('Local / custom endpoint')

    fireEvent.click(row)

    await waitFor(() => expect(startManualLocalEndpoint).toHaveBeenCalledWith(null))
  })
})

describe('OAuth account API routing', () => {
  it('saves the Guardian checkbox for only its originating Codex account and profile', async () => {
    const value = provider('openai-codex', true, {
      supports_account_management: true,
      accounts: [
        { id: 'first', label: 'Personal', priority: 0, guardian_enabled: false },
        { id: 'second', label: 'Work', priority: 1, guardian_enabled: true }
      ]
    })

    bindConfigReadOrigin(value, { connectionId: 'connection-a', profile: 'alpha' })
    listOAuthProviders.mockResolvedValue({ providers: [value] })
    render(<ProvidersSettings onClose={vi.fn()} onViewChange={vi.fn()} view="accounts" />)
    const personal = await screen.findByRole('listitem', { name: 'Personal' })
    const work = screen.getByRole('listitem', { name: 'Work' })
    const checkbox = within(personal).getByRole('checkbox', { name: 'Codex Guardian (experimental)' })
    expect(checkbox.getAttribute('data-state')).toBe('unchecked')
    expect(within(work).getByRole('checkbox', { name: 'Codex Guardian (experimental)' }).getAttribute('data-state')).toBe('checked')
    fireEvent.click(checkbox)
    await waitFor(() => expect(updateOAuthAccount).toHaveBeenCalledWith(
      'openai-codex', 'first', { guardian_enabled: true }, expect.objectContaining({ profile: 'alpha', connectionId: 'connection-a' })
    ))
  })

  it('pins every account request to the connection and profile that served its list', async () => {
    const api = vi.fn(async (request: { method?: string }) =>
      request.method ? { ok: true } : { providers: [provider('openai-codex', true)] }
    )

    const previous = window.hermesDesktop
    const real = await import('@/api/config')
    const client = await import('@/api/client')
    Object.defineProperty(window, 'hermesDesktop', { configurable: true, value: { api } })

    try {
      client.setApiRequestConnection('connection-a')
      const response = await real.listOAuthProviders('beta')
      const origin = real.peekConfigReadOrigin(response.providers[0])
      client.setApiRequestConnection('connection-b')
      await real.updateOAuthAccount('openai-codex', 'stable/first', { enabled: false }, origin)
      await real.deleteOAuthAccount('openai-codex', 'stable/second', origin)
      await real.linkOAuthAccount('openai-codex', 'team', 'stable/third', origin)
      await real.listOAuthProviders(origin)
      client.setApiRequestConnection('connection-a')

      expect(api.mock.calls.map(([request]) => request)).toEqual([
        expect.objectContaining({ connectionId: 'connection-a', profile: 'beta', path: '/api/providers/oauth' }),
        expect.objectContaining({
          connectionId: 'connection-a',
          profile: 'beta',
          method: 'PATCH',
          path: '/api/providers/oauth/openai-codex/accounts/stable%2Ffirst',
          body: { enabled: false }
        }),
        expect.objectContaining({
          connectionId: 'connection-a',
          profile: 'beta',
          method: 'DELETE',
          path: '/api/providers/oauth/openai-codex/accounts/stable%2Fsecond'
        }),
        expect.objectContaining({
          connectionId: 'connection-a',
          profile: 'beta',
          method: 'POST',
          path: '/api/providers/oauth/openai-codex/accounts/link',
          body: { owner_profile: 'team', account_id: 'stable/third' }
        }),
        expect.objectContaining({ connectionId: 'connection-a', profile: 'beta', path: '/api/providers/oauth' })
      ])
    } finally {
      client.setApiRequestConnection(null)

      if (previous) {
        Object.defineProperty(window, 'hermesDesktop', { configurable: true, value: previous })
      } else {
        Reflect.deleteProperty(window, 'hermesDesktop')
      }
    }
  })
})
