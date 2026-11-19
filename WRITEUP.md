# Assessment writeup - working draft

## Approach

I separate current website observations from CRM account and billing history.
Full-population matching produces evidence-backed relationship groups before
proposing field changes. A rebrand is not automatically a new facility;
acquisition is not automatically a duplicate; co-location, splitting, and
consolidation require identity review.

The review workbench presents source facts, related accounts, financial context,
and exact steps. Both approval and rejection persist. Billing protection uses
the required AND condition, and uncertain creates are reconciled rather than retried.

## AI assistance

AI assisted with requirements interpretation, implementation, synthetic examples,
and automated checks. The candidate must review the code, understand its decisions,
and describe the actual live-review work honestly. There are no LLM calls inside
the pipeline.

## Current status and limitations

This is an offline build with synthetic accounts. Live collection, create/update
contract verification, actual CRM corrections, and the real demonstration recording
are not completed. No historical acquisition, split, or consolidation is claimed
without evidence. Complex relationships that the available scalar pointers cannot
represent are surfaced instead of fabricated.

## What I would build next

After owner confirmation: verify live API behavior, connect the same review flow
to the isolated CRM, and complete evidence-based corrections. Beyond this exercise:
API-supported conditional writes/idempotency keys, richer historical ownership
evidence, and clearer representation of genuine split/consolidation relationships.

## Actual time and results

To be completed by the candidate using an honest record of actual work.
Do not substitute the exercise's approximate two-hour estimate.
