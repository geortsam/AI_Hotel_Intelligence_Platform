# Test fixtures

## `hotel_booking_demand_sample.csv`

**This is a test fixture, not a dataset.** It exists so that the offline pipeline can be tested
against the real source's *shapes* — its column names, its English month names, its status
vocabulary, its zero-night rows — without any test reaching the network.

**Its demand numbers are not hotel history and must never be read as such.** It is a 1-in-400
sample, so a date that really sold 180 room nights appears here with one or two. Nothing in the
repository quotes a number from this file.

### Exactly how it was derived

From the source file recorded in
[`ml/pipelines/offline_demand.py`](../../../ml/pipelines/offline_demand.py) —
`hotels.csv` at TidyTuesday commit `d75aaa0d31596ad6487ae0db20138067d301e031`, SHA-256
`7c2ae42a7353905ea136e5c2287f17c92c5435826598bfbb8491c6f0c7b1fc06`:

1. keep the header line;
2. split the data rows into the `Resort Hotel` block and the `City Hotel` block, preserving
   file order;
3. take every 400th row of each block (`block[::400]`);
4. write the Resort rows then the City rows.

That yields 101 + 199 = 300 data rows. **Every line is verbatim from the source** — no column
was dropped, renamed, reordered or rewritten, which is what makes the derivation checkable
rather than merely described.

A stride rather than a head slice because the pipeline's coverage rule needs a wide arrival
window: the first few hundred rows of a block all arrive in the same fortnight, and the rule
correctly refuses to emit any target date for them. The stride spans 2015-07 to 2017-08 and
costs 43 KB instead of 225 KB.

### Licence

Unchanged from the source: the underlying data is published under
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) with Antonio, N., de Almeida, A., &
Nunes, L. (2019), *Hotel booking demand datasets*, Data in Brief 22, 41–49,
[doi:10.1016/j.dib.2018.11.126](https://doi.org/10.1016/j.dib.2018.11.126). See
[`docs/ml-training-data.md`](../../../docs/ml-training-data.md).

### Line endings

`.gitattributes` sets `* text=auto`, so this file is checked out with CRLF on Windows and LF on
Linux. Tests therefore read it through `csv`, which accepts both, and **never checksum its
bytes**. The checksums the pipeline cares about are taken over output it serialises itself,
with LF written explicitly.
