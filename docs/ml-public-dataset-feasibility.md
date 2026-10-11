# Public hotel-demand datasets: feasibility audit — R2.4

> **Research only.** No dataset was downloaded or committed, and no model, feature, protocol,
> record or served artifact changed. Sources were consulted on **2026-10-11**; every licence or
> provenance detail below cites the page it came from, and anything that could not be confirmed is
> marked **UNKNOWN**.

## 1. What a new dataset would have to fix

R2.1–R2.2b left five limitations that more modelling on `demand_daily_v1` cannot remove:

| # | Limitation | Where it was established |
|---|---|---|
| L1 | Only **two hotels**, so "hotel-held-out" means one transfer per direction and nothing more | R2.2 §3, R2.2b §5.4 |
| L2 | **No capacity**: observed maxima (226, 187) cannot be read as room counts, so demand cannot be normalised across hotels | R2.3 H1; R2.2b §2 (`rooms_existing_at_cutoff` is empty) |
| L3 | **No low-demand or small-property rows**: 0 of 590 held-out targets below 40; the ≈165.83 weak regime is untested | R2.2 §3, R2.3 H4 |
| L4 | **No unused period**: every month (2015-07 .. 2017-08) has now been seen by Stage 6.3, R2.2 and R2.2b | R2.2 §1 |
| L5 | **Same source** for training and evaluation, so no result says anything about other hotels | every R2 record's `claims` |

R2.2b additionally showed that cross-hotel errors are one-signed level offsets (city → resort
+32.941, resort → city −40.319 on test). Testing whether a level- or capacity-normalised target
removes them needs **L1 and L2** together: many hotels, each with a known room count.

## 2. Ranked shortlist

Ranked by how much of L1–L5 a candidate could address **with its terms verified**, not by
convenience. Licence and provenance cells cite their evidence; nothing is inferred from a name.

| Rank | Candidate | Source | Licence / access (evidence) | Provenance (evidence) |
|---|---|---|---|---|
| 1 | **Hotel booking demand datasets** (Antonio, de Almeida & Nunes, *Data in Brief* 22, 2019, doi:10.1016/j.dib.2018.11.126) — the current source | [Europe PMC record PMC6297060](https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=DOI:10.1016/j.dib.2018.11.126&format=json&resultType=core) | Open access, licence field `cc by` (Europe PMC metadata) | Real bookings of a resort hotel (H1, 40,060 rows) and a city hotel (H2, 79,330 rows), arrivals 2015-07-01 .. 2017-08-31; hotel and customer identifiers deleted (abstract, same record) |
| 2 | **Author-held multi-hotel daily data**: Ampountolas & Legg 2024, *J. Revenue and Pricing Management* (doi:10.1057/s41272-023-00445-7); Huang, Li, Zheng et al. 2022/23, *IJCHM* (doi:10.1108/IJCHM-12-2021-1505) | [Springer](https://link.springer.com/article/10.1057/s41272-023-00445-7); [Emerald](https://www.emerald.com/insight/content/doi/10.1108/IJCHM-12-2021-1505/full/html) | Ampountolas & Legg: per a search-result excerpt of the article, data "available from the authors upon reasonable request" and "used under license" (the article page itself returned a login redirect, so the statement was **not read first-hand**). Huang et al.: **UNKNOWN** (paywalled; the abstract page shows "Licensed re-use rights only") | Ampountolas & Legg: daily occupancy for multiple US urban properties (count **UNKNOWN**). Huang et al.: daily demand of **118 hotels**, Xiamen, Jan–Jun 2019 and Jan–Jun 2021 (Emerald abstract); provider **UNKNOWN** |
| 3 | **CoStar/STR SHARE Center** (academic programme) | [STR SHARE Center](https://str.com/training/academic-resources/share-center-details) (returned HTTP 403); [Glion library terms](https://library.glion.edu/?p=35443) | Academic membership via an institution. One member library's terms: classroom, student-project or research use only; not to be shown to outsiders or used for profit. STR's own terms: **UNKNOWN** (page not readable) | Occupancy, ADR, RevPAR, supply and room counts are offered (search excerpts of STR material). Whether **property-level** daily rows are given to academics: **UNKNOWN** — the STR material found describes aggregation for confidentiality |
| 4 | **Inside Airbnb** calendars | [Get the data](https://insideairbnb.com/get-the-data/); [Data assumptions](https://insideairbnb.com/data-assumptions/); [Data policies](https://insideairbnb.com/data-policies/) | "licensed under a Creative Commons Attribution 4.0 International License" (get-the-data page); but the data-policies page asks users **"Do not republish the data"**, and only about the last 12 months is free — older data needs an archived-data request | Scraped Airbnb listings, not hotels. The calendar "does not differentiate between a booked night vs an unavailable night"; occupancy is **modelled** from reviews (50 % review rate, assumed stay length, 70 % cap) (data-assumptions page) |
| 5 | **Eurostat `tour_occ_nim`** — nights spent at tourist accommodation, monthly | [Eurostat API](https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/tour_occ_nim?format=JSON&lang=EN&geo=PT&unit=NR&c_resid=TOTAL&nace_r2=I551&lastTimePeriod=2); [copyright notice](https://ec.europa.eu/eurostat/web/main/help/copyright-notice) | CC BY 4.0, "provided the source is acknowledged" (copyright notice) | Official statistics, aggregated by country/region and activity (e.g. Portugal, NACE I551 "Hotels and similar accommodation", 2026-07: 7,844,008 nights); no establishment rows (API response) |

### Examined and rejected

| Candidate | Source | Why rejected (evidence) |
|---|---|---|
| Kaggle "Hotel Reservations Dataset" (ahsan81) | [Kaggle API metadata](https://www.kaggle.com/api/v1/datasets/view/ahsan81/hotel-reservations-classification-dataset) | Uploader-declared CC BY 4.0, but the description states **no original source, no hotel count and no date range**. Provenance **UNKNOWN**; unusable as evidence |
| Kaggle "Hotel booking demand" (jessemostipak) | [Kaggle API metadata](https://www.kaggle.com/api/v1/datasets/view/jessemostipak/hotel-booking-demand) | Re-upload: "originally from the article Hotel Booking Demand Datasets" — the same two hotels as rank 1 |
| Zenodo 17612645 "Hotel Booking Reservation" | [Zenodo API](https://zenodo.org/api/records/17612645) | "updated and cleaned version of the original UCI 'Hotel Booking Demand' dataset" — again rank 1's data, modified by a third party |
| Lisbon hotel customers 2015–2018 (*Data in Brief*, doi:10.1016/j.dib.2020.106583) | [Europe PMC](https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=%22customers%20personal%2C%20behavioral%2C%20demographic%22%20Lisbon&format=json&resultType=core) | One hotel; one row per **customer** (83,590), no daily target or capacity; licence `cc by-nc-nd` |
| Saito et al. 2019 online-booking data (Mendeley doi:10.17632/9tkynnhj23.1) | [Mendeley Data](https://data.mendeley.com/datasets/9tkynnhj23/1); [RePEc record](https://ideas.repec.org/a/eee/ininma/v46y2019icp37-53.html) | CC BY 4.0, but two Tokyo luxury hotels, booking-site data crawled for a discrete-choice model; not occupancy; contents of the deposit not described on its page |
| "AtliQ Grands / AtliQ Hospitality" (codebasics) | search results only | No primary source found; widely used as a training case study. Real or simulated: **UNKNOWN**. Excluded until a primary source says |

## 3. Criteria comparison

✔ satisfies · ✘ fails · ? UNKNOWN · ~ partial

| Criterion | 1 Antonio 2019 | 2 Author-held | 3 STR SHARE | 4 Inside Airbnb | 5 Eurostat |
|---|---|---|---|---|---|
| Distinct properties | ✘ 2 | ✔ 118 (Huang); ? (Ampountolas) | ? | ✔ thousands (not hotels) | ✘ none (aggregates) |
| Daily granularity | ✔ (derived) | ✔ | ? | ~ daily calendar snapshots | ✘ monthly |
| Occupied-room-night target | ✔ derived from bookings | ✔ demand/occupancy | ✔ occupancy (level ?) | ✘ booked and blocked indistinguishable | ✘ nights spent, aggregated |
| Capacity / inventory | ✘ | ? | ~ room counts offered; per-property ? | ✘ | ✘ |
| Property IDs for hotel-held-out | ~ 2 anonymous | ? | ? | ✔ listing IDs | ✘ |
| Low-demand / small properties | ✘ (0 held-out targets < 40) | ? | ? | ✔ many small listings (not hotels) | ✘ |
| Missingness documented | ✔ | ? | ? | ~ snapshot; host calendars may be stale | ✔ official flags |
| Licence verified | ✔ CC BY (Europe PMC) | ✘ on request / ? | ✘ institution-licensed; STR terms ? | ✔ CC BY 4.0, but "do not republish" | ✔ CC BY 4.0 |
| Redistributable in this repository | ✔ | ✘ | ✘ | ✘ by the publisher's request | ✔ |
| Realistic forecast cutoff | ✔ (Stage 6.1 contract) | ? | ? | ✘ no booking timestamps | ~ monthly, published with a lag |
| Real observed data | ✔ | ✔ (per articles) | ✔ | ✔ scraped; occupancy modelled | ✔ official |
| Unused periods for a temporal holdout | ✘ all months seen (L4) | ✔ | ✔ | ✔ | ✔ |
| Fit for a portfolio/university project | ✔ | ~ only with the authors' agreement | ~ only inside the member institution | ~ only on its own terms | ✔ as context only |

## 4. Which R2.2b limitations each candidate could address

| Candidate | L1 hotels | L2 capacity | L3 low demand | L4 unused period | L5 other source | Net |
|---|---|---|---|---|---|---|
| 1 Antonio 2019 | ✘ | ✘ | ✘ | ✘ | ✘ | Exhausted for evaluation; remains the training and development source |
| 2 Author-held | ✔ (Huang) | ? | ? | ✔ | ✔ | Strongest scientifically, but not public: cannot be committed or re-run by CI |
| 3 STR SHARE | ? | ~ | ? | ✔ | ✔ | Could be strong; terms forbid sharing outside the institution, so not reproducible here |
| 4 Inside Airbnb | ~ (listings) | ✘ | ~ | ✔ | ✔ | Different target and population; a proxy at best, and the publisher asks that it not be republished |
| 5 Eurostat | ✘ | ✘ | ✘ | ✔ | ✔ | Context only (seasonality, regional level); cannot evaluate a hotel-level model |

## 5. Conclusion

**No suitable public candidate has been verified.** None of the sources found is at once
(a) multi-hotel, (b) daily, (c) an occupied-room-night target, (d) with capacity or property IDs,
and (e) clearly licensed for redistribution. The one dataset that is public, real and verified
(rank 1) is the one already in use, and every month of it has been evaluated.

So no download is justified in this stage, and none was made. What could close L1–L5 is
access-controlled: author-held data (rank 2) or an institutional STR licence (rank 3). Either would
need an explicit decision about reproducibility, because neither could be committed or re-run in
CI. A dataset like that would also need its own records kept out of the repository.

## 6. Proposed evaluation design for a future stage

Written so that it can be pre-registered unchanged once a dataset's terms are verified. It fixes
nothing about *which* dataset.

* **Pre-registration.** A new versioned protocol (the `frozen_holdout_v1` / `hotel_transfer_v1`
  convention), committed with its checksum before any score is computed.
* **Target.** Occupied room nights per property per day; **plus occupancy = room nights ÷ rooms
  available** wherever capacity is known, so hotels of different sizes can be compared (R2.2b's
  offsets are level offsets).
* **Cutoff.** 7 days, as now: features knowable at D − 7; lags k ≥ 7, reconstructed and checked as
  R2.2 does. Any on-the-books feature needs booking-creation timestamps; without them it is
  excluded, not approximated.
* **Splits — both, reported separately, never pooled.**
  1. **Temporal holdout**: train on early months, validate, then test on months **no earlier
     stage has seen**.
  2. **Hotel-held-out**: properties partitioned **by ID** into train and held-out groups before
     any look at their outcomes, stratified by size (capacity bands, including small properties).
     Held-out hotels are scored only in the temporal test window.
* **Same-hotel controls.** For every held-out hotel, a model fitted on that hotel's own training
  months at matched size (the R2.2b control), so transfer is separated from data volume.
* **Baselines.** `seasonal_naive_7` of the evaluated hotel on identical eligible rows; also a
  seasonal naive on occupancy where capacity exists.
* **Metrics.** MAE, RMSE, sMAPE (current definitions); mean signed error (prediction − actual);
  prediction and training-target ranges; win proportion; breakdowns by hotel, by capacity band, by
  pre-registered lag ranges, and a **low-demand segment defined on inputs** (lags), matching the
  Stage 6.9 `below_calibration` rule rather than R2.2's actual-based one.
* **Minimums.** At least 30 paired rows per group (as now); the number of held-out hotels fixed in
  advance.
* **Claims.** Same false-by-default claims block; anything learned is about the hotels in that
  dataset.

## 7. Unresolved risks and the evidence that would resolve them

| Risk | Evidence needed |
|---|---|
| Ampountolas & Legg data terms read only through a search excerpt | The article's data-availability statement read first-hand, and the authors' written terms for academic reuse and publication of derived results |
| Huang et al. (118 hotels) data source and availability unknown | The full text's data-availability statement; the provider's (e.g. a booking platform's) reuse terms |
| STR SHARE property-level availability and terms unknown | STR's current SHARE membership agreement for this institution: property-level vs aggregate, daily vs monthly, whether derived metrics may be published, whether room counts are included |
| No public source for small or low-demand properties | A dataset with capacity and property sizes, including properties under about 40 room nights a day |
| Inside Airbnb CC BY vs "do not republish" | Written confirmation from Inside Airbnb that storing a derived subset in a public repository is acceptable; irrelevant unless an availability proxy is ever wanted |
| Kaggle ahsan81 provenance | A primary source (organisation, collection method, dates); without one it stays excluded |
| Whether any access-controlled data can be evaluated reproducibly here | A decision that records may be produced from data that is not committed, with the dataset checksum recorded but the rows kept out of the repository |

## 8. What did not change

No model was trained, refitted, tuned or scored. `demand_baseline_v1`, `APPROVED_MODEL`,
`demand_daily_v1` and its manifest, the `frozen_holdout_v1` and `hotel_transfer_v1` protocols and
records, the database schema, `daily_hotel_metrics`, tags and releases are untouched. No demo or
preview database was accessed.
