import { formatCount, formatDate, UNAVAILABLE } from '@/lib/format'
import type { ForecastAccuracyResponse, SegmentAccuracy } from '@/types/mlPerformance'

import styles from './AccuracyPanel.module.css'

/**
 * Measured forecast error, exactly as `/ml/forecast-accuracy` reported it.
 *
 * ## Every number here came off the wire
 *
 * No metric is computed, rounded, rescaled or combined in this component. MAE, RMSE and sMAPE
 * are rendered as the server sent them; the counts are the server's counts. There is no
 * arithmetic in this file at all.
 *
 * ## The two segments are never merged
 *
 * `below_calibration` and `within_calibration` are rendered as two separate tables with their
 * own denominators, and there is deliberately **no combined figure** — not hidden, not
 * collapsed behind a toggle, not available at all. The protocol splits them because the model
 * behaves differently across them: Stage 6.6 measured that it cannot distinguish hotels at or
 * below roughly forty room nights a night. A single headline number would average those two
 * populations into one that describes neither, and the API returns no field one could travel
 * in.
 *
 * A model version's entry shows both segments even when one scored nothing, because "no
 * observations in this segment" is itself the answer to a question a reader is entitled to
 * ask.
 *
 * ## `null` is rendered as absent, never as zero
 *
 * A segment that scored no predictions has no error. Printing `0.0` would read as a perfect
 * one, which is the single most misleading thing this panel could do.
 *
 * ## What is stated on screen rather than assumed
 *
 * * the **settlement lag**, in the body text — not a tooltip — because every figure here is
 *   conditional on it, and a reader who does not know that dates inside the lag were excluded
 *   cannot interpret the denominators;
 * * whether the measurement is **provisional**, when an allocation covering the scored window
 *   can still change status;
 * * the protocol version and checksum that produced the figures;
 * * the server's own claim boundary, verbatim.
 */

export interface AccuracyPanelProps {
  readonly accuracy: ForecastAccuracyResponse
}

/**
 * One metric value, grouped for reading. `null` prints the shared absence glyph, never a zero.
 *
 * `formatCount` is the application's shared number formatter and is used here for the same
 * reason it is used everywhere else: it groups thousands and caps the fraction digits for
 * display. It changes how a figure is *written*, never what it is — the value passed in is the
 * one the server computed, and no metric is derived, scaled or combined in this file.
 *
 * The suffix is a label, not arithmetic. sMAPE arrives from `ml/metrics.py` already multiplied
 * by 100 (`100 * mean(...)`, range 0..200), so the per-cent sign names the scale the server
 * already used rather than converting to it.
 */
function metric(value: number | null, suffix = ''): string {
  return value === null ? UNAVAILABLE : `${formatCount(value)}${suffix}`
}

function SegmentTable({ segment, label }: { segment: SegmentAccuracy; label: string }) {
  const { metrics } = segment

  return (
    <div className={styles.segment}>
      <h5 className={styles.segmentTitle}>{label}</h5>
      <table className={styles.table}>
        <caption className={styles.caption}>
          {`${label}: measured error over ${formatCount(metrics.observations)} scored prediction(s). This segment has its own denominator and is never combined with the other.`}
        </caption>
        <tbody>
          <tr>
            <th scope="row">Scored predictions</th>
            <td>{formatCount(metrics.observations)}</td>
          </tr>
          <tr>
            <th scope="row">
              Skipped
              <span className={styles.hint}>no recorded occupancy to score against</span>
            </th>
            <td>{formatCount(metrics.skipped)}</td>
          </tr>
          <tr>
            <th scope="row">
              MAE<span className={styles.hint}>mean absolute error, room nights</span>
            </th>
            <td>{metric(metrics.mae)}</td>
          </tr>
          <tr>
            <th scope="row">
              RMSE<span className={styles.hint}>root mean squared error, room nights</span>
            </th>
            <td>{metric(metrics.rmse)}</td>
          </tr>
          <tr>
            <th scope="row">
              sMAPE
              <span className={styles.hint}>
                symmetric mean absolute percentage error, range 0–200
              </span>
            </th>
            <td>{metric(metrics.smape, '%')}</td>
          </tr>
        </tbody>
      </table>
    </div>
  )
}

export function AccuracyPanel({ accuracy }: AccuracyPanelProps) {
  const scored =
    accuracy.scored_from === null || accuracy.scored_to === null
      ? null
      : `${formatDate(accuracy.scored_from)} to ${formatDate(accuracy.scored_to)}`

  return (
    <div className={styles.panel}>
      {/*
        The claims boundary, first and in ordinary type — not a footnote under the numbers.
        The sentence is the server's own `measurement.statement`, rendered verbatim rather than
        summarised: a paraphrase is a new claim, and the whole point of the field is that the
        claim is the API's to make.
      */}
      <p className={styles.caveat} data-testid="accuracy-caveat">
        <strong>Predictive accuracy has not been established.</strong> {accuracy.measurement.statement}
      </p>

      <dl className={styles.facts}>
        <div className={styles.fact}>
          <dt>Settlement lag</dt>
          {/* On screen, in the panel body. A figure whose eligibility rule is hidden in a
              tooltip is a figure most readers will interpret wrongly. */}
          <dd data-testid="settlement-lag">
            {`${formatCount(accuracy.settlement_lag_days)} days`}
            <span className={styles.hint}>
              a target date is scored only once it is this far past, so late-recorded bookings
              are already in the ground truth
            </span>
          </dd>
        </div>
        <div className={styles.fact}>
          <dt>Measured as of</dt>
          <dd>{formatDate(accuracy.as_of_date)}</dd>
        </div>
        <div className={styles.fact}>
          <dt>Dates actually scored</dt>
          <dd>
            {scored ?? (
              <span className={styles.absent}>
                None — no date in this window had cleared the settlement lag
              </span>
            )}
          </dd>
        </div>
        <div className={styles.fact}>
          <dt>Predictions in window</dt>
          <dd>{formatCount(accuracy.candidates)}</dd>
        </div>
        <div className={styles.fact}>
          <dt>Excluded by settlement lag</dt>
          <dd>{formatCount(accuracy.ineligible_by_settlement)}</dd>
        </div>
        <div className={styles.fact}>
          <dt>From another model identity</dt>
          <dd>{formatCount(accuracy.out_of_scope_model_digest)}</dd>
        </div>
      </dl>

      {accuracy.settled ? null : (
        <p className={styles.provisional} role="status">
          <strong>Provisional.</strong>{' '}
          {`${formatCount(accuracy.unsettled_allocations)} room allocation(s) covering the scored window can still change status, so these figures may move.`}
        </p>
      )}

      {accuracy.by_model_version.length === 0 ? (
        <p className={styles.empty} role="status">
          No predictions in this window were scored, so there is no error to report. This is a
          normal answer for a property whose forecasts are newer than the settlement lag.
        </p>
      ) : (
        accuracy.by_model_version.map((entry) => (
          <section
            key={entry.model_version}
            className={styles.version}
            aria-label={`Measured error for model version ${entry.model_version}`}
          >
            <h4 className={styles.versionTitle}>{entry.model_version}</h4>
            <div className={styles.segments}>
              <SegmentTable
                segment={entry.below_calibration}
                label="At or below 40 room nights"
              />
              <SegmentTable segment={entry.within_calibration} label="Above 40 room nights" />
            </div>
          </section>
        ))
      )}

      <p className={styles.provenance}>
        {`Protocol ${accuracy.measurement.protocol_version}, checksum ${accuracy.measurement.protocol_checksum.slice(0, 12)}…`}
      </p>
    </div>
  )
}
