import styles from './AuthLoading.module.css'

/**
 * Shown while the session is being restored.
 *
 * Deliberately says nothing about the property and shows no figures. The application does
 * not yet know who is asking, so anything resembling business data here would be either
 * invented or the previous user's -- and this screen appears on a shared back-office
 * machine between shifts.
 *
 * `role="status"` with `aria-live="polite"` announces the wait once, without interrupting.
 */
export function AuthLoading() {
  return (
    <div className={styles.screen}>
      <div className={styles.inner} role="status" aria-live="polite">
        <span className={styles.spinner} aria-hidden="true" />
        <span>Restoring your session…</span>
      </div>
    </div>
  )
}
