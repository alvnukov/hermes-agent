import type { TranslationOverrides } from './define-locale'

export const frProviders: NonNullable<NonNullable<TranslationOverrides['settings']>['providers']> = {
      addAccount: 'Ajouter un compte',
      accountLoginHint: 'Dans le navigateur, connectez-vous à l’autre compte ChatGPT avant de valider le code.',
      accountPriority: position => `Priorité ${position}`,
      accountName: 'Nom du compte',
      useAccount: 'Utiliser dans ce profil',
      renameAccount: 'Renommer le compte',
      moveAccountUp: 'Monter',
      moveAccountDown: 'Descendre',
      accountOwnerProfile: profile => `Du profil : ${profile}`,
      accountStatus: {
        ready: 'Prêt',
        disabled: 'Désactivé',
        exhausted: 'Limite atteinte',
        dead: 'Connexion requise',
        unavailable: 'Indisponible'
      },
      ownerAccountDisabled: 'Le compte source est désactivé dans le profil de son propriétaire.',
      accountUnavailableHint: 'Le compte source n’est plus disponible.',
      deleteAccount: 'Supprimer le compte',
      unlinkAccount: 'Dissocier du profil',
      deleteAccountConfirm: account => `Supprimer ${account} ?`,
      unlinkAccountConfirm: account => `Dissocier ${account} de ce profil ?`,
      deleteAccountHint:
        'Les profils liés à ce compte peuvent perdre leur accès. Reconnectez-vous pour l’ajouter à nouveau.',
      unlinkAccountHint: 'Ce profil cessera d’utiliser le compte. Le compte source sera conservé.',
      existingAccount: 'Compte existant',
      linkAccount: 'Lier le compte',
      accountWriteFailed: 'Impossible de modifier ce compte.',
      noAccounts: 'Aucun fournisseur de compte disponible.',
      connectAccount: 'Connecter un compte',
      haveApiKey: 'Vous avez une clé API ?',
      intro:
        "Connectez-vous avec un abonnement — pas de clé API à copier. Hermes lance la connexion navigateur pour vous, directement dans l'application.",
      connected: 'Connecté',
      collapse: 'Réduire',
      connectAnother: 'Connecter un autre fournisseur',
      otherProviders: 'Autres fournisseurs',
      disconnect: 'Déconnecter',
      disconnectInTerminal: 'Déconnecter (exécute la commande de suppression dans le terminal)',
      removeConfirm: provider => `Supprimer ${provider} ?`,
      removeExternalGeneric: provider => `${provider} est géré par sa propre CLI — supprimez-le là-bas.`,
      removeKeyManaged: provider => `${provider} est configuré depuis une clé API. Supprimez-le depuis les clés API.`,
      removeTerminalConfirm: (provider, command) =>
        `Déconnecter ${provider} ? Cela exécute « ${command} » dans le terminal pour effacer l'identifiant.`,
      removeTerminalRunning: provider => `Exécution de la déconnexion ${provider} dans le terminal…`,
      removedTitle: 'Compte supprimé',
      removedMessage: provider => `${provider} a été supprimé.`,
      failedRemove: provider => `Impossible de supprimer ${provider}`,
      noProviderKeys: 'Aucune clé API de fournisseur disponible.',
      searchKeys: 'Rechercher des fournisseurs…',
      noKeysMatch: 'Aucun fournisseur ne correspond à votre recherche.',
      localEndpoint: {
        title: 'Point de terminaison local / personnalisé',
        description:
          "Pointez Hermes vers n'importe quel point de terminaison compatible OpenAI (Zyphra, vLLM, llama.cpp, Ollama, etc)."
      },
      loading: 'Chargement des fournisseurs...'
    }
