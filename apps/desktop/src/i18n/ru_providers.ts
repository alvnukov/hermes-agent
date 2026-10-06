import type { TranslationOverrides } from './define-locale'

export const ruProviders: NonNullable<NonNullable<TranslationOverrides['settings']>['providers']> = {
      addAccount: 'Добавить аккаунт',
      accountLoginHint: 'В браузере войдите в другой аккаунт ChatGPT перед подтверждением кода.',
      accountPriority: position => `Приоритет ${position}`,
      accountName: 'Название аккаунта',
      useAccount: 'Использовать в этом профиле',
      guardianEnabled: 'Codex Guardian (экспериментально)',
      guardianHint: 'Проверяет действия через этот аккаунт. Без галочки используются проверки Hermes.',
      renameAccount: 'Переименовать аккаунт',
      moveAccountUp: 'Поднять',
      moveAccountDown: 'Опустить',
      accountOwnerProfile: profile => `Из профиля: ${profile}`,
      accountStatus: {
        ready: 'Готов',
        disabled: 'Отключён',
        exhausted: 'Лимит исчерпан',
        dead: 'Нужен вход',
        unavailable: 'Недоступен'
      },
      ownerAccountDisabled: 'Исходный аккаунт отключён в профиле владельца.',
      accountUnavailableHint: 'Исходный аккаунт больше недоступен.',
      deleteAccount: 'Удалить аккаунт',
      unlinkAccount: 'Отвязать от профиля',
      deleteAccountConfirm: account => `Удалить ${account}?`,
      unlinkAccountConfirm: account => `Отвязать ${account} от этого профиля?`,
      deleteAccountHint:
        'Связанные с аккаунтом профили могут потерять доступ. Чтобы добавить его снова, войдите заново.',
      unlinkAccountHint: 'Этот профиль перестанет использовать аккаунт. Исходный аккаунт сохранится.',
      existingAccount: 'Существующий аккаунт',
      linkAccount: 'Привязать аккаунт',
      accountWriteFailed: 'Не удалось изменить аккаунт.',
      noAccounts: 'Нет доступных провайдеров для входа.',
      connectAccount: 'Подключить аккаунт',
      haveApiKey: 'Ввести API-ключ вместо этого?',
      intro:
        'Войдите по подписке — копировать API-ключ не нужно. Hermes проведёт вход в браузере прямо здесь, в приложении.',
      connected: 'Подключено',
      collapse: 'Свернуть',
      connectAnother: 'Подключить другой провайдер',
      otherProviders: 'Другие провайдеры',
      disconnect: 'Отключить',
      disconnectInTerminal: 'Отключить (выполнит команду удаления в терминале)',
      removeConfirm: provider => `Удалить ${provider}?`,
      removeExternalGeneric: provider => `${provider} управляется собственным CLI — удалите его там.`,
      removeKeyManaged: provider => `${provider} настроен по API-ключу. Удалите его в разделе API-ключи.`,
      removeTerminalConfirm: (provider, command) =>
        `Отключить ${provider}? В терминале будет выполнена команда "${command}" для сброса учётных данных.`,
      removeTerminalRunning: provider => `Выполняется отключение ${provider} в терминале…`,
      removedTitle: 'Аккаунт удалён',
      removedMessage: provider => `${provider} удалён.`,
      failedRemove: provider => `Не удалось удалить ${provider}`,
      noProviderKeys: 'API-ключи провайдеров недоступны.',
      searchKeys: 'Поиск провайдеров…',
      noKeysMatch: 'Провайдеры, подходящие под поиск, не найдены.',
      localEndpoint: {
        title: 'Локальный / свой эндпоинт',
        description: 'Направьте Hermes на любой OpenAI-совместимый эндпоинт (Zyphra, vLLM, llama.cpp, Ollama и т. д.).'
      },
      loading: 'Загрузка провайдеров…'
    }
