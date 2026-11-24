# SBOM checker fixtures (A3)

`invalid-component-type.cdx.json` is the positive control CI's image-reproducibility job runs
before it checks the real SBOMs: `python scripts/sbom_check.py expect-invalid` must reject it.
It is CycloneDX 1.6 in every field the checker reads before the schema -- `bomFormat`,
`specVersion` and the generator (Syft 1.57.1) -- and breaks the schema in exactly one place: a
component whose `type` is not in the schema's enum. A validator that accepts anything therefore
fails the job. (The schema forbids extra top-level members, so this note cannot live in the file.)
