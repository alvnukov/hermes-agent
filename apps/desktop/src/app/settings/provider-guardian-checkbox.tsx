import { Checkbox } from '@/components/ui/checkbox'
import { useI18n } from '@/i18n'
import type { OAuthAccount } from '@/types/hermes'

export function GuardianAccountCheckbox({
  account,
  management,
  onChange,
  pending,
  providerId
}: {
  account: OAuthAccount
  management: boolean
  onChange: (enabled: boolean) => void
  pending: boolean
  providerId: string
}) {
  const { t } = useI18n()
  const copy = t.settings.providers

  if (!management || providerId !== 'openai-codex') {
    return null
  }

  return (
    <label className="flex items-start gap-2">
      <Checkbox
        aria-label={copy.guardianEnabled}
        checked={account.guardian_enabled === true}
        disabled={pending || account.missing}
        onCheckedChange={checked => onChange(checked === true)}
      />
      <span className="grid gap-1">
        <span>{copy.guardianEnabled}</span>
        <span className="text-xs text-muted-foreground">{copy.guardianHint}</span>
      </span>
    </label>
  )
}
