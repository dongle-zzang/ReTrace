// UI translation helpers (unit-tested in tests/i18n.test.cjs).
// The English text is the message key, so untranslated text still reads correctly and messages
// built in lib/composables (validation, API errors) can be translated where they are shown.

export type Locale = 'ko' | 'en'
export const LOCALES: readonly Locale[] = ['ko', 'en']
export type MessageParams = Record<string, string | number>
export interface Catalog {
  messages: Record<string, string>
  /** For runtime text with variable parts, e.g. validation messages built in lib/. */
  patterns?: [RegExp, string][]
}

export function isLocale(value: unknown): value is Locale {
  return value === 'ko' || value === 'en'
}

/** Stored choice first, then the browser language; anything that is not Korean falls back to English. */
export function initialLocale(stored: unknown, languages: readonly string[] = []): Locale {
  if (isLocale(stored)) return stored
  return languages.some(language => language.toLowerCase().startsWith('ko')) ? 'ko' : 'en'
}

export function interpolate(template: string, params?: MessageParams) {
  if (!params) return template
  return template.replace(/\{(\w+)\}/g, (match, name: string) => name in params ? String(params[name]) : match)
}

export function translate(catalog: Catalog | null, text: string, params?: MessageParams) {
  if (!catalog) return interpolate(text, params)
  const message = catalog.messages[text]
  if (message !== undefined) return interpolate(message, params)
  for (const [pattern, replacement] of catalog.patterns ?? []) {
    if (pattern.test(text)) return interpolate(text.replace(pattern, replacement), params)
  }
  return interpolate(text, params)
}
