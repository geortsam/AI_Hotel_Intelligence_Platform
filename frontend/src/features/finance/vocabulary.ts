import type { RecurrenceInterval } from '@/types/finance'

/**
 * The words this application puts on ledger concepts.
 *
 * Every label here is presentation over a value the backend owns. Nothing in this file
 * decides anything: it does not gate a control, does not choose an endpoint and does not
 * change a figure. A code with no entry falls back to the code itself, because inventing a
 * friendly name for a category the catalogue added after this file was written would be
 * showing a label the server never said.
 */

/** The two journals. Used to pick a service call and a heading, never to compute anything. */
export type LedgerKind = 'revenue' | 'expenses'

export interface LedgerVocabulary {
  /** The tab, and the section heading. */
  readonly title: string
  /** One line under the heading, describing what the journal is. */
  readonly summary: string
  /** Singular, for "Post a revenue line". */
  readonly lineNoun: string
  /** The column heading over `revenue_date` / `expense_date`. */
  readonly dateLabel: string
  /** The column heading over the free-text reference field, which is named differently. */
  readonly referenceLabel: string
}

export const LEDGER_VOCABULARY: Readonly<Record<LedgerKind, LedgerVocabulary>> = {
  revenue: {
    title: 'Revenue',
    summary:
      'Every revenue line posted against this property, newest first. The journal is append-only: a mistake is corrected by posting a negative line, never by editing one.',
    lineNoun: 'revenue line',
    dateLabel: 'Revenue date',
    referenceLabel: 'Reference',
  },
  expenses: {
    title: 'Expenses',
    summary:
      'Every expense line posted against this property, newest first. The journal is append-only: a mistake is corrected by posting a credit note, never by editing one.',
    lineNoun: 'expense line',
    dateLabel: 'Expense date',
    referenceLabel: 'Invoice reference',
  },
}

/** The three values `recurrence_interval` may take. */
export const RECURRENCE_INTERVALS: readonly RecurrenceInterval[] = [
  'monthly',
  'quarterly',
  'annual',
]

const RECURRENCE_LABELS: Readonly<Record<RecurrenceInterval, string>> = {
  monthly: 'Monthly',
  quarterly: 'Quarterly',
  annual: 'Annual',
}

export function recurrenceLabel(interval: RecurrenceInterval): string {
  return RECURRENCE_LABELS[interval]
}
