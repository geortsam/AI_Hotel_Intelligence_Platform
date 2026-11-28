# Backend architecture — moved

> **Redirect.** This page was the backend's stage snapshot at Stage 3B.12. On 2026-11-25
> (roadmap R6.1) its content was consolidated into [architecture.md](architecture.md), which is
> maintained; the snapshot as last written is in the repository's history at `2a07405`
> (`git show 2a07405:docs/backend-architecture.md`). Code comments and stage records that cite
> this path find each section here:

| Section of the snapshot | Now in |
|---|---|
| 1. Domains | [architecture.md §2.1](architecture.md#21-domains) |
| 2. Layering | [architecture.md §2.2](architecture.md#22-what-each-layer-owns-and-how-that-is-enforced) |
| 3. Transaction boundaries, and conditional updates (Issue H6) | [architecture.md §2.3](architecture.md#23-transaction-boundaries-and-conditional-updates) |
| 4. Tenant isolation | [architecture.md §2.4](architecture.md#24-tenant-isolation-in-detail) |
| 5. Identifiers, and two credentials, one identity (R1.1) | [architecture.md §2.5](architecture.md#25-identifiers-and-credentials) |
| 6. Database-authoritative constraints | [architecture.md §2.6](architecture.md#26-guarantees-the-database-owns) |
| 7. Analytics and Intelligence | [architecture.md §5](architecture.md#5-data-and-intelligence-architecture) |
| 8. Error handling | [architecture.md §4.1](architecture.md#41-error-handling) |
| 9. Known schema debt | [the database README §4](../database/README.md#4-relationships-and-cardinality) and [§5](../database/README.md#what-the-constraints-deliberately-do-not-prevent) |
| 10. Known limitations | [architecture.md §8.1](architecture.md#81-known-limitations-of-the-backend) |
| 11. Remaining production requirements | [architecture.md §8.2](architecture.md#82-the-production-requirements-stage-3b12-listed-and-where-each-stands) |
| 12. Deliberately not present | [architecture.md §8.3](architecture.md#83-what-is-deliberately-absent) |
