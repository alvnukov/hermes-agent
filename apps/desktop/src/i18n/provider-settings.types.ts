export interface ProviderSettingsTranslations {
      addAccount: string
      accountLoginHint: string
      accountPriority: (position: number) => string
      accountName: string
      useAccount: string
      renameAccount: string
      moveAccountUp: string
      moveAccountDown: string
      accountOwnerProfile: (profile: string) => string
      accountStatus: { ready: string; disabled: string; exhausted: string; dead: string; unavailable: string }
      ownerAccountDisabled: string
      accountUnavailableHint: string
      deleteAccount: string
      unlinkAccount: string
      deleteAccountConfirm: (account: string) => string
      unlinkAccountConfirm: (account: string) => string
      deleteAccountHint: string
      unlinkAccountHint: string
      existingAccount: string
      linkAccount: string
      accountWriteFailed: string
      noAccounts: string
      connectAccount: string
      haveApiKey: string
      intro: string
      connected: string
      collapse: string
      connectAnother: string
      otherProviders: string
      disconnect: string
      disconnectInTerminal: string
      removeConfirm: (provider: string) => string
      removeExternalGeneric: (provider: string) => string
      removeKeyManaged: (provider: string) => string
      removeTerminalConfirm: (provider: string, command: string) => string
      removeTerminalRunning: (provider: string) => string
      removedTitle: string
      removedMessage: (provider: string) => string
      failedRemove: (provider: string) => string
      noProviderKeys: string
      searchKeys: string
      noKeysMatch: string
      localEndpoint: {
        title: string
        description: string
      }
      loading: string
    }
