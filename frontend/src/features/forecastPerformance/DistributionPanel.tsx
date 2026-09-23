import { formatCount, formatDate, UNAVAILABLE } from '@/lib/format'
import type {
  PredictionDistributionResponse,
  SegmentSummary,
  WindowSummary,
} from '@/types/mlPerformance'

import styles from './DistributionPanel.module.css'

/**
 * What the model's inputs and outputs looked like over a window, as the API summarised them.
 *
 * ## This describes; it does not decide
 *
 * The `distribution_v1` protocol contains no threshold, no verdict, no alert and no ranking, so
 * neither does this component. **The word "drift" does not appear on screen**, and no movement
 * is coloured, flagged, or called significant — deciding what a difference means would be a
 * judgement the protocol explicitly declines to make, and a UI that made it anyway would be
 * inventing a capability the platform does not have.
 *
 * ## Nothing is computed here
 *
 * Every count, minimum, maximum, mean, median and quantile is rendered as the server sent it.
 * No quantile is interpolated, no mean is taken, no field is aggregated across segments or
 * model versions, and there is no arithmetic in this file.
 *
 * ## Observed and comparison stay apart
 *
 * The observed window and the baseline comparison are two separate blocks with their own
 * headings. A difference is labelled as a difference and shown with its sign; it is never
 * displayed where an observed value belongs. When `comparison` is `null` — no baseline was
 * requested — the component says exactly that rather than rendering zeros, because a set of
 * zero differences would read as "nothing moved", which is a different and unsupported claim.
 */

export interface DistributionPanelProps {
  readonly distribution: PredictionDistributionResponse
}

/** A value the server may legitimately have sent as `null`. Absent is not zero. */
function value(input: number | null): string {
  return input === null ? UNAVAILABLE : formatCount(input)
}

/** A signed difference. The sign is the server's; nothing is recomputed to produce it. */
function difference(input: number | null): string {
  if (input === null) {
    return UNAVAILABLE
  }
  // `>` is a comparison for choosing a label, not arithmetic on the value.
  return input > 0 ? `+${formatCount(input)}` : formatCount(input)
}

function SegmentFields({
  segment,
  label,
  signed,
}: {
  readonly segment: Pick<SegmentSummary, 'observations' | 'fields'>
  readonly label: string
  readonly signed: boolean
}) {
  const render = signed ? difference : value

  return (
    <div className={styles.segment}>
      <h5 className={styles.segmentTitle}>
        {label}
        <span className={styles.count}>
          {signed
            ? `${difference(segment.observations)} predictions`
            : `${formatCount(segment.observations)} predictions`}
        </span>
      </h5>
      <div className={styles.tableScroll}>
        <table className={styles.table}>
          <caption className={styles.caption}>
            {signed
              ? `${label}: change from the baseline window, per field. Target minus baseline, as the server computed it.`
              : `${label}: distribution of each model input and of the prediction, over the observed window.`}
          </caption>
          <thead>
            <tr>
              <th scope="col">Field</th>
              <th scope="col">Count</th>
              <th scope="col">Min</th>
              <th scope="col">Median</th>
              <th scope="col">Mean</th>
              <th scope="col">Max</th>
            </tr>
          </thead>
          <tbody>
            {segment.fields.map((field) => (
              <tr key={field.field}>
                <th scope="row" className={styles.fieldName}>
                  {field.field}
                </th>
                <td>{signed ? difference(field.count) : formatCount(field.count)}</td>
                <td>{render(field.minimum)}</td>
                <td>{render(field.median)}</td>
                <td>{render(field.mean)}</td>
                <td>{render(field.maximum)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function WindowBlock({ window, signedFields }: { window: WindowSummary; signedFields: boolean }) {
  return (
    <>
      <p className={styles.windowMeta}>
        {`${formatDate(window.window_from)} to ${formatDate(window.window_to)} — `}
        {`${formatCount(window.candidates)} prediction(s)`}
        {window.out_of_scope_model_digest === 0
          ? ''
          : `, ${formatCount(window.out_of_scope_model_digest)} from another model identity and summarised into nothing`}
      </p>
      {window.by_model_version.length === 0 ? (
        <p className={styles.empty} role="status">
          No stored predictions in this window, so there is nothing to summarise.
        </p>
      ) : (
        window.by_model_version.map((entry) => (
          <section
            key={entry.model_version}
            className={styles.version}
            aria-label={`Prediction distribution for model version ${entry.model_version}`}
          >
            <h4 className={styles.versionTitle}>{entry.model_version}</h4>
            <SegmentFields
              segment={entry.below_calibration}
              label="At or below 40 room nights"
              signed={signedFields}
            />
            <SegmentFields
              segment={entry.within_calibration}
              label="Above 40 room nights"
              signed={signedFields}
            />
          </section>
        ))
      )}
    </>
  )
}

export function DistributionPanel({ distribution }: DistributionPanelProps) {
  return (
    <div className={styles.panel}>
      <p className={styles.caveat}>
        <strong>These are descriptions, not findings.</strong>{' '}
        {distribution.measurement.statement}
      </p>

      <section className={styles.block} aria-label="Observed window">
        <h3 className={styles.blockTitle}>Observed window</h3>
        <WindowBlock window={distribution.observed} signedFields={false} />
      </section>

      <section className={styles.block} aria-label="Comparison against a baseline window">
        <h3 className={styles.blockTitle}>Comparison</h3>
        {distribution.comparison === null ? (
          /*
           * The documented absence state. No baseline window was requested, so there is no
           * comparison -- which is not the same as a comparison whose differences are all
           * zero, and must not be rendered as one.
           */
          <p className={styles.empty} role="status" data-testid="no-comparison">
            No baseline window was requested, so there is nothing to compare against. This is
            not the same as finding no change.
          </p>
        ) : (
          <>
            <h4 className={styles.subTitle}>Baseline</h4>
            <WindowBlock window={distribution.comparison.baseline} signedFields={false} />
            <h4 className={styles.subTitle}>Change from baseline</h4>
            {distribution.comparison.by_model_version.map((entry) => (
              <section
                key={entry.model_version}
                className={styles.version}
                aria-label={`Change from baseline for model version ${entry.model_version}`}
              >
                <h5 className={styles.versionTitle}>{entry.model_version}</h5>
                <SegmentFields
                  segment={entry.below_calibration}
                  label="At or below 40 room nights"
                  signed
                />
                <SegmentFields
                  segment={entry.within_calibration}
                  label="Above 40 room nights"
                  signed
                />
              </section>
            ))}
          </>
        )}
      </section>

      <p className={styles.provenance}>
        {`Protocol ${distribution.measurement.protocol_version}, checksum ${distribution.measurement.protocol_checksum.slice(0, 12)}…`}
      </p>
    </div>
  )
}
