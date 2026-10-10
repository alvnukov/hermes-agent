import type { TranslationOverrides } from './define-locale'

export const deProviders: NonNullable<NonNullable<TranslationOverrides['settings']>['providers']> = {
  addAccount: 'Konto hinzufügen',
  accountLoginHint: 'Melde dich im Browser beim anderen ChatGPT-Konto an, bevor du den Code bestätigst.',
  accountPriority: position => `Priorität ${position}`,
  accountName: 'Kontoname',
  useAccount: 'In diesem Profil verwenden',
  guardianEnabled: 'Codex Guardian (experimentell)',
  guardianHint: 'Prüft Aktionen mit diesem Konto. Ohne Häkchen prüft Hermes.',
  renameAccount: 'Konto umbenennen',
  moveAccountUp: 'Nach oben',
  moveAccountDown: 'Nach unten',
  accountOwnerProfile: profile => `Aus Profil: ${profile}`,
  accountStatus: {
    ready: 'Bereit',
    disabled: 'Deaktiviert',
    exhausted: 'Limit erreicht',
    dead: 'Anmeldung nötig',
    unavailable: 'Nicht verfügbar'
  },
  ownerAccountDisabled: 'Das Quellkonto ist im Profil des Eigentümers deaktiviert.',
  accountUnavailableHint: 'Das Quellkonto ist nicht mehr verfügbar.',
  deleteAccount: 'Konto löschen',
  unlinkAccount: 'Vom Profil trennen',
  deleteAccountConfirm: account => `${account} löschen?`,
  unlinkAccountConfirm: account => `${account} von diesem Profil trennen?`,
  deleteAccountHint:
    'Verknüpfte Profile können den Zugriff verlieren. Melde dich erneut an, um das Konto wieder hinzuzufügen.',
  unlinkAccountHint: 'Dieses Profil verwendet das Konto nicht mehr. Das Quellkonto bleibt erhalten.',
  existingAccount: 'Vorhandenes Konto',
  linkAccount: 'Konto verknüpfen',
  accountWriteFailed: 'Das Konto konnte nicht geändert werden.',
  noAccounts: 'Keine Kontoanbieter verfügbar.',
  connectAccount: 'Ein Konto verbinden',
  haveApiKey: 'Haben Sie stattdessen einen API-Key?',
  intro:
    'Melden Sie sich mit einem Abo an – kein API-Key zum Kopieren. Hermes übernimmt die Browser-Anmeldung für Sie, direkt hier in der App.',
  connected: 'Verbunden',
  collapse: 'Einklappen',
  connectAnother: 'Weiteren Provider verbinden',
  otherProviders: 'Weitere Provider',
  disconnect: 'Trennen',
  disconnectInTerminal: 'Trennen (führt den Entfernungsbefehl im Terminal aus)',
  removeConfirm: provider => `${provider} entfernen?`,
  removeExternalGeneric: provider => `${provider} wird von einer eigenen CLI verwaltet – entfernen Sie ihn dort.`,
  removeKeyManaged: provider => `${provider} ist über einen API-Key konfiguriert. Entfernen Sie ihn unter API-Keys.`,
  removeTerminalConfirm: (provider, command) =>
    `${provider} trennen? Dadurch wird "${command}" im Terminal ausgeführt, um die Zugangsdaten zu löschen.`,
  removeTerminalRunning: provider => `${provider}-Trennung wird im Terminal ausgeführt…`,
  removedTitle: 'Konto entfernt',
  removedMessage: provider => `${provider} wurde entfernt.`,
  failedRemove: provider => `${provider} konnte nicht entfernt werden`,
  noProviderKeys: 'Keine Provider-API-Keys verfügbar.',
  searchKeys: 'Provider suchen…',
  noKeysMatch: 'Keine Provider entsprechen Ihrer Suche.',
  localEndpoint: {
    title: 'Lokaler / eigener Endpoint',
    description:
      'Verbinden Sie Hermes mit einem beliebigen OpenAI-kompatiblen Endpunkt (Zyphra, vLLM, llama.cpp, Ollama usw.).'
  },
  loading: 'Provider werden geladen…'
}
