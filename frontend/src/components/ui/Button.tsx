import type { ButtonHTMLAttributes, ReactNode } from 'react'

import styles from './Button.module.css'

export type ButtonVariant = 'primary' | 'secondary' | 'ghost' | 'danger'
export type ButtonSize = 'sm' | 'md' | 'lg'

interface BaseButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  readonly variant?: ButtonVariant
  readonly size?: ButtonSize
  readonly children?: ReactNode
  /**
   * The action is in flight (UI-4). The button is disabled and `aria-busy`, a spinner
   * replaces the label visually, and the label stays in place -- transparent, not removed --
   * so the button keeps both its accessible name and its width, and nothing beside it moves.
   */
  readonly loading?: boolean
}

/**
 * A button with a visible label. `iconOnly` may be omitted or false.
 */
interface LabelledButtonProps extends BaseButtonProps {
  readonly iconOnly?: false
}

/**
 * A square button showing only an icon.
 *
 * `aria-label` is required by the type, not by convention. An icon-only control is silent
 * to a screen reader, and "remember to add a label" is a rule that gets forgotten exactly
 * once per codebase; making it a compile error is cheaper than finding it in an audit.
 */
interface IconButtonProps extends BaseButtonProps {
  readonly iconOnly: true
  readonly 'aria-label': string
}

export type ButtonProps = LabelledButtonProps | IconButtonProps

/**
 * The application's button.
 *
 * A real `<button>`, always. Anything that changes the URL is a `<Link>` instead, so that
 * middle-click, right-click and "open in new tab" keep working the way the browser
 * promises -- a `<div onClick>` that navigates breaks all three and is invisible to
 * assistive technology.
 *
 * `type` defaults to `"button"` rather than the HTML default of `"submit"`. That default is
 * a real trap: a button inside a form that only meant to toggle a panel will submit the
 * form instead, and a form's actual submit button can say `type="submit"` explicitly.
 */
export function Button({
  variant = 'secondary',
  size = 'md',
  iconOnly = false,
  type = 'button',
  className,
  children,
  loading = false,
  disabled,
  ...rest
}: ButtonProps) {
  const classes = [
    styles.button,
    styles[variant],
    styles[size],
    iconOnly ? styles.iconOnly : undefined,
    loading ? styles.loading : undefined,
    className,
  ]
    .filter(Boolean)
    .join(' ')

  if (!loading) {
    return (
      <button type={type} className={classes} disabled={disabled} {...rest}>
        {children}
      </button>
    )
  }

  return (
    <button type={type} className={classes} disabled aria-busy="true" {...rest}>
      <span className={styles.spinner} aria-hidden="true" />
      <span className={styles.label}>{children}</span>
    </button>
  )
}
