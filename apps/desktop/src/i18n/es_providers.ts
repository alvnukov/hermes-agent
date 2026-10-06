import type { TranslationOverrides } from './define-locale'

export const esProviders: NonNullable<NonNullable<TranslationOverrides['settings']>['providers']> = {
      addAccount: 'Añadir cuenta',
      accountLoginHint: 'En el navegador, inicia sesión en la otra cuenta de ChatGPT antes de confirmar el código.',
      accountPriority: position => `Prioridad ${position}`,
      accountName: 'Nombre de la cuenta',
      useAccount: 'Usar en este perfil',
      guardianEnabled: 'Codex Guardian (experimental)',
      guardianHint: 'Revisa acciones con esta cuenta. Desactivado, Hermes revisa las acciones.',
      renameAccount: 'Renombrar cuenta',
      moveAccountUp: 'Subir',
      moveAccountDown: 'Bajar',
      accountOwnerProfile: profile => `Del perfil: ${profile}`,
      accountStatus: {
        ready: 'Lista',
        disabled: 'Desactivada',
        exhausted: 'Límite agotado',
        dead: 'Requiere iniciar sesión',
        unavailable: 'No disponible'
      },
      ownerAccountDisabled: 'La cuenta de origen está desactivada en el perfil de su propietario.',
      accountUnavailableHint: 'La cuenta de origen ya no está disponible.',
      deleteAccount: 'Eliminar cuenta',
      unlinkAccount: 'Desvincular del perfil',
      deleteAccountConfirm: account => `¿Eliminar ${account}?`,
      unlinkAccountConfirm: account => `¿Desvincular ${account} de este perfil?`,
      deleteAccountHint:
        'Los perfiles vinculados pueden perder el acceso. Inicia sesión de nuevo para volver a añadir la cuenta.',
      unlinkAccountHint: 'Este perfil dejará de usar la cuenta. La cuenta de origen se conservará.',
      existingAccount: 'Cuenta existente',
      linkAccount: 'Vincular cuenta',
      accountWriteFailed: 'No se pudo modificar esta cuenta.',
      noAccounts: 'No hay proveedores de cuentas disponibles.',
      connectAccount: 'Conectar una cuenta',
      haveApiKey: '¿Tienes una clave API?',
      intro:
        'Inicia sesión con una suscripción, sin copiar claves API. Hermes ejecuta el inicio de sesión del navegador por ti, aquí mismo en la app.',
      connected: 'Conectado',
      collapse: 'Contraer',
      connectAnother: 'Conectar otro proveedor',
      otherProviders: 'Otros proveedores',
      disconnect: 'Desconectar',
      disconnectInTerminal: 'Desconectar (ejecuta el comando de eliminación en el terminal)',
      removeConfirm: provider => `¿Eliminar ${provider}?`,
      removeExternalGeneric: provider => `${provider} se gestiona con su propia CLI; elimínalo allí.`,
      removeKeyManaged: provider => `${provider} se configura con una clave API. Quítalo en Claves API.`,
      removeTerminalConfirm: (provider, command) =>
        `¿Desconectar ${provider}? Esto ejecutará “${command}” en el terminal para borrar la credencial.`,
      removeTerminalRunning: provider => `Ejecutando la desconexión de ${provider} en el terminal…`,
      removedTitle: 'Cuenta eliminada',
      removedMessage: provider => `Se eliminó ${provider}.`,
      failedRemove: provider => `No se pudo eliminar ${provider}`,
      noProviderKeys: 'No hay claves API de proveedores disponibles.',
      searchKeys: 'Buscar proveedores…',
      noKeysMatch: 'Ningún proveedor coincide con tu búsqueda.',
      localEndpoint: {
        title: 'Endpoint local o personalizado',
        description:
          'Conecta Hermes con cualquier endpoint compatible con OpenAI (Zyphra, vLLM, llama.cpp, Ollama, etc.).'
      },
      loading: 'Cargando proveedores...'
    }
