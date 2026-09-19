import { ref } from 'vue'

export type Theme = 'dark' | 'light'

const STORAGE_KEY = 'sssf-theme'

function preferred(): Theme {
  try {
    const stored = localStorage.getItem(STORAGE_KEY)
    if (stored === 'dark' || stored === 'light') return stored
  } catch {
    /* private mode — fall through to the OS preference */
  }
  return window.matchMedia?.('(prefers-color-scheme: light)').matches ? 'light' : 'dark'
}

function apply(theme: Theme) {
  document.documentElement.dataset.theme = theme
}

/** Reactive theme with persistence. The initial value is read synchronously
 * so the first paint already matches — index.html sets the same attribute
 * even earlier to avoid any flash. */
export function useTheme() {
  const theme = ref<Theme>(preferred())
  apply(theme.value)

  function toggle() {
    theme.value = theme.value === 'dark' ? 'light' : 'dark'
    apply(theme.value)
    try {
      localStorage.setItem(STORAGE_KEY, theme.value)
    } catch {
      /* private mode — the session keeps the choice */
    }
  }

  return { theme, toggle }
}
