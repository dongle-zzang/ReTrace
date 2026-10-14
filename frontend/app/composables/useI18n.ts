import { computed } from 'vue'
import { initialLocale, translate, type Locale, type MessageParams } from '~/lib/i18n'
import { ko } from '~/lib/i18n.ko'

const STORAGE_KEY = 'retrace.locale'

function readStoredLocale() {
  try { return localStorage.getItem(STORAGE_KEY) } catch { return null }
}

export function useI18n() {
  // CSR only (ssr: false), so the browser state is available on first use.
  const locale = useState<Locale>('locale', () => initialLocale(readStoredLocale(), typeof navigator === 'undefined' ? [] : navigator.languages ?? [navigator.language]))
  /** BCP 47 tag for toLocale*String. */
  const dateLocale = computed(() => locale.value === 'ko' ? 'ko-KR' : 'en-US')

  function setLocale(next: Locale) {
    locale.value = next
    try { localStorage.setItem(STORAGE_KEY, next) } catch { /* private mode: keep it for this tab only */ }
  }

  function t(text: string, params?: MessageParams) {
    return translate(locale.value === 'ko' ? ko : null, text, params)
  }

  return { locale, dateLocale, setLocale, t }
}
