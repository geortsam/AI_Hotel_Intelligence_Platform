# ML production runtime — how the approved model reaches the image

**Stage 6.7.** Stage 6.6 built a serving boundary that answered `503` inside the production
image, because that image had neither an ML runtime nor a model. This document is about closing
that gap, and about the one design question it forced into the open.

Nothing about the model changed. `demand_baseline_v1` is the artifact Stage 6.5 fitted and
Stage 6.4 accepted, the dataset is untouched, the feature version is untouched, and the
acceptance policy is untouched. **Being deployed is not an accuracy claim** — see §9.

---

## 1. The problem, and why the obvious answer was wrong

The payload is not committed. `.gitignore` has excluded model weights since Stage 1, a pickle
is arbitrary code on load, and distributing one through `git clone` is exactly what the Stage
6.5 trust boundary exists to prevent. So the image cannot copy the model out of the repository;
it has to **regenerate** it from the committed dataset and the pinned configuration.

That makes one question decisive: *is the thing it regenerated the approved model?*

The obvious answer — compare the payload's SHA-256 against the one Stage 6.5 recorded — is
measurably wrong. Refitting this model on one machine, from one dataset, with one seed, produces
a different payload digest for every OpenMP thread count:

| OpenMP threads | payload SHA-256 | bytes | canonical model digest |
|---|---|---|---|
| 1 | `3e9986c5…` | 711,530 | `436bf6b3…` |
| 2 | `b2c5802a…` | 711,530 | `436bf6b3…` |
| 4 | `d5b21ccd…` | 711,530 | `436bf6b3…` |
| 8 | `fb751fe4…` | 711,530 | `436bf6b3…` |
| **12** | **`bdfeb3b8…`** ← the Stage 6.5 record | 711,530 | `436bf6b3…` |

Twelve is the physical core count of the machine Stage 6.5 was built on. It is **not recorded in
`artifact.json`**, which names the interpreter, the platform, scikit-learn, NumPy, SciPy and the
pickle protocol — but not the thread count. So `bdfeb3b8…` identifies a CPU topology as much as
it identifies a model, and requiring a Linux build container to reproduce it would be requiring
it to have twelve cores.

Every one of those builds has the **same canonical model digest**, because that is what Stage
6.5 built the digest for: a hash over the feature columns, the estimator configuration, the
training extent and the model's predictions on a fixed synthetic probe grid, each formatted to
six decimal places. It is a fingerprint of *what the model computes*.

> **The payload SHA-256 is therefore an environment fact, recorded, and the canonical model
> digest is the identity, enforced.** Stage 6.5 said as much when it introduced the digest:
> *"no claim is made that [byte reproducibility] holds across interpreter, scikit-learn or NumPy
> builds; the canonical digest is the cross-build claim."*

**Nothing was weakened to reach that position.** The payload digest is still verified — against
the metadata generated beside it, before any deserialisation — and twenty approved values are
checked at build time that no digest comparison would have covered.

---

## 2. How the artifact enters the image

Three stages in `backend/Dockerfile`:

```
base              the pinned interpreter and the runtime dependencies
    |
artifact-builder  DISPOSABLE. Has the training dataset and the whole offline `ml/` package.
    |             Refits the approved model, verifies it, writes /artifact/{model.pkl,artifact.json}
    v
runtime           what ships: the application, the migrations, thirteen `ml/` modules,
                  and the two files copied out of the builder. No dataset.
```

The training dataset therefore exists **in the build** and does not exist **in the image**. That
is the entire reason the middle stage is there.

The artifact lands at `/app/ml/models/demand_baseline_v1/`, which is where
`ml.artifact.DEFAULT_ARTIFACT_DIRECTORY` resolves to — `<ml package>/models/<model version>`.
No environment variable, no setting and no code change was needed to point the runtime at it,
and **there is still no path a request can name**.

### What the image contains, and what it does not

| Present | Absent |
|---|---|
| `app/` (the application) | `ml/data/` — no raw, processed or external dataset |
| `alembic.ini`, `database/migrations/` | `ml/notebooks/`, `ml/manifests/` |
| 13 `ml/` modules — the import closure of `ml.artifact` and `ml.inference` | the 5 pipeline entry points that fit, evaluate, validate or package a model |
| `ml/models/demand_baseline_v1/model.pkl` and `artifact.json` | `requirements-dev.txt` and everything in it |
| scikit-learn 1.9.1 and its own requirements | pandas, torch, any other ML library |

The thirteen modules are **computed, not chosen**:
`test_the_dockerfile_ships_exactly_the_import_closure` walks the import graph from
`ml.artifact` and `ml.inference` and requires the Dockerfile's allowlist to equal it exactly. A
module added to the closure without being copied would make the image fail on import; one
copied without being needed is training code that should not ship.

`ml/pipelines/offline_demand.py` is the awkward member of that list. It is dataset-building
code, and it ships because `ml.loading` imports a single helper from it — not because inference
uses it. It can build nothing, because no dataset ships, and a CI step proves the dataset is
absent.

---

## 3. Runtime dependencies

One package was added to `backend/requirements.txt`:

```
scikit-learn==1.9.1
```

The same pin `ml/requirements-ml.txt` has carried since Stage 6.3, so the model is served by the
library version it was fitted with rather than a nearby one; a test asserts the two agree.

It is needed for exactly two things: **deserialising** the artifact (the pickle names
`sklearn.ensemble._hist_gradient_boosting` classes, so the module must be importable before the
object exists) and calling **`predict`**.

NumPy, SciPy, joblib, threadpoolctl, narwhals and cloudpickle arrive as scikit-learn's own
requirements and are **not declared separately**. Two shipped modules import NumPy and SciPy by
name — `ml/artifact.py` and `ml/manifests.py` — but only to read `__version__` into metadata.
Pinning them beside scikit-learn would assert a combination scikit-learn has not been tested
against. **pandas is required nowhere at inference time and is installed nowhere.**

The cost is real: the image grows by roughly 150 MB. That is recorded rather than hidden, and
CI prints the built image's size on every run.

---

## 4. Build-time integrity verification

`ml/pipelines/build_production_artifact.py` runs inside the disposable stage and **fails the
build** — no warning path, no flag that turns a check off — unless all twenty match:

| | |
|---|---|
| schema version | `artifact_v1` |
| model name, model version | `demand_baseline`, `demand_baseline_v1` |
| feature version, dataset version | against the **code constants** in `app.ml.dataset`, then against the metadata |
| dataset SHA-256 | the Stage 6.2 dataset's |
| feature columns **and their order** | the nine Stage 6.3 columns |
| forecast horizon | 7 |
| estimator configuration, and its SHA-256 | `bbaf2881…` |
| artifact format | `pickle` |
| claims | all four `false` |
| **probe predictions** | the 64 approved values, recomputed from the freshly fitted estimator |
| **canonical model digest** | `436bf6b3cc5f1a2cc1a2e7fa5e971cedcd0405aa5b7f07a84967b293118a8f70` |
| training extent | partition rows, rows reaching `fit`, start, end, date count, hotels |

The two version checks are worth a note. They compare the metadata against the constants in
`app.ml.dataset`, not against themselves. An earlier draft passed the metadata's own
`feature_version` into `train_artifact` and then compared the result to the metadata — so a
tampered `feature_version` travelled straight through. A test caught it; the versions now come
from code the metadata cannot influence.

Seventeen tampering cases are exercised in `tests/backend/test_production_packaging.py`, one per
approved value, and each must refuse the build and write no payload.

---

## 5. Runtime integrity verification

**Unchanged from Stage 6.6, and not relaxed because a build-time check now exists.** The nine
pre-deserialisation checks in `ml.artifact.load_artifact` and the seven post-deserialisation
checks in `app/ml/artifact_store.py` all still run on every process start —
including the probe-prediction comparison that asks the unpickled estimator what it computes.
See [ml-serving.md](ml-serving.md) §6.

The production artifact passes them without a single exemption: its `sha256` is its own
payload's, its canonical digest is the approved one, and its claims are the approved four.

---

## 6. Failure behaviour

If the artifact is missing, truncated, swapped or edited inside the image:

* the application **still starts** — a bad model is not a boot failure;
* the demand endpoint answers `503 MODEL_UNAVAILABLE`, one fixed sentence;
* no path, filename, digest, library name or traceback reaches the client;
* `X-Request-ID` behaviour is untouched, and the rest of the API is unaffected;
* **nothing is downloaded.** There is no fallback model, no remote fetch and no URL to fetch
  from. CI proves this by loading the model in a container started with `--network none` and
  with `socket.socket` replaced by a function that raises.

CI exercises this for real: it derives a throwaway image from the built one, truncates the
payload inside it, runs it against the live database, and asserts the 503, the error code, the
absence of leaks and the intact request id. **The production artifact is never touched.**

### Rollback

The image is the unit of rollback. Because the artifact is built into it, deploying the previous
image tag restores the previous model with no separate artifact step and no state to unwind —
there is nothing in the database, no cache to clear and no external store to roll back. If a
build produces a model that does not match the approved identity, no image is produced at all,
so there is nothing to roll back from.

### Replacing the model in a future stage

1. Fit and accept the new model offline, exactly as Stages 6.3–6.5 did, producing a new
   `ml/models/<version>/artifact.json` with a new canonical digest.
2. Update `APPROVED_MODEL` in `backend/app/ml/serving.py` — the approval lives in reviewed
   application code, not in the artifact.
3. Update the Dockerfile's `COPY --from=artifact-builder … ./ml/models/<version>/`.
4. The build then refuses anything that is not the new approved model, and the runtime refuses
   anything whose canonical digest is not the new pinned one.

No step in that list involves publishing a binary, and none involves a network fetch.

### Why runtime downloads are forbidden

A model fetched at container startup is a model chosen by whatever answered the request. It
makes the running system depend on a remote service's availability and integrity, it means two
replicas of the same image can serve different models, it puts arbitrary-code-on-load behind a
network boundary the image cannot verify offline, and it makes the image no longer a complete
description of what runs. The artifact is built in, verified twice, and immutable in the image —
the runtime user cannot even write to it.

---

## 7. Offline training and online inference

| | **Offline training** | **Build-time packaging** | **Online inference** |
|---|---|---|---|
| Where | a developer machine, `ml/` | the disposable Docker stage | the running container |
| Reads | the committed dataset | the committed dataset | one hotel's room nights, from PostgreSQL |
| Calls | `fit` | `fit`, once, to regenerate | `predict`, and only `predict` |
| Produces | the approved identity | a payload that must match it | one number, with its provenance |
| Fails by | being rejected in review | failing the image build | returning 422 or 503 |

The middle column is new in Stage 6.7 and is the only place `fit` runs in the delivery path. It
runs before the image exists, in a stage that is thrown away, and its output is admitted only if
it is indistinguishable from the approved model by twenty measures.

---

## 8. Reproducibility

The repository's existing mechanism is unchanged: the image-reproducibility job builds the
backend image twice with `--no-cache`, hashes every file under `/app` and site-packages inside
both, and requires them byte-identical.

Two consequences shaped this stage:

* **The shipped `artifact.json` carries no wall clock.** It is the approved metadata with the
  payload digest and byte count replaced, plus a `production_build` block recording the
  environment. A `created_at` in that block would make two builds of one commit differ, and a
  test forbids one.
* **The payload must be deterministic within a machine**, which it is: the fit is seeded, and
  two builds on one runner use the same thread count.

Independent builds are proven to agree on what matters —
`test_two_independent_builds_share_the_canonical_identity` and
`test_two_independent_builds_predict_identically` build the artifact twice and require the same
canonical digest, the same probe predictions and the same predictions.

---

## 9. What is still not established

Everything [ml-serving.md](ml-serving.md) §9 says remains true. Deployment changed where the
model runs, not how good it is.

1. **Production accuracy is not established.** Stage 6.4 accepted this model against a
   predeclared *offline* policy about reproducibility, identity and leakage — deliberately not
   about accuracy, and with no "must beat the baseline" criterion.
2. **The model still cannot tell small hotels apart.** A flat history of 1, 3, 5, 10 or 40 room
   nights a night all produce the same prediction, ≈ 165.83, because all of them fall below the
   lowest bin edge learned from two hotels whose daily demand runs to the hundreds. It carries
   no hotel identity feature and no capacity normalisation. **Cross-hotel generalisation is not
   established**, and packaging it for production does not make it transferable.
3. **Twenty-eight days of recorded occupancy are required**, specifically on *T*−7, *T*−14 and
   *T*−28.
4. **~~Predictions are not persisted~~ — corrected.** That was true of Stage 6.7 and is no
   longer true of the repository. Stage 6.8 persists every served prediction with the model
   identity that produced it, through migration `0010_demand_predictions`; Stage 6.9 measures
   error against realised demand under a declared protocol; Stage 6.10 makes the input and
   output distributions observable. **Still absent: drift *detection*, thresholds, alerting and
   retraining** — none of those arrived with any of those stages, and none is implied by them.
   See [ml-prediction-persistence-design.md](ml-prediction-persistence-design.md),
   [ml-accuracy-measurement.md](ml-accuracy-measurement.md) and
   [ml-drift-observation.md](ml-drift-observation.md).
5. **The image is ~150 MB larger.** That is the price of the runtime, and it is worth restating
   when the model it carries is an offline research candidate.

---

*See also:* [ml-serving.md](ml-serving.md) for the API and the runtime boundary,
[ml-model-card.md](ml-model-card.md) for what the model is,
[deployment/reproducibility.md](deployment/reproducibility.md) for the image pins.
