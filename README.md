# Bellhaven Review Workbench

A local, approval-first workbench for reconciling facility identities and current
parent relationships without destroying billing history.

**Current delivery: offline review build.** The app uses synthetic data only.
It does not load a credential or connect to the assessment website or CRM.
The owner must confirm before live integration testing. A working demo is not
a corrected assessment CRM, a submitted assessment, or a recorded demo video.

## Run locally

Python 3.11 or newer on macOS/Linux:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock.txt
.venv/bin/python -m pip install --no-deps -e .
.venv/bin/streamlit run app.py
```

Open the loopback address printed by Streamlit. The server binds to `127.0.0.1`,
and usage-statistics collection is disabled.

The demo contains a rebrand, a direct parent correction, a financially protected
CHOW, a missing account, duplicate candidates, a shared-campus/split ambiguity,
an existing historical CHOW, missing financial data, and an unlisted account.
All facilities, addresses, balances, and source references in the demo are synthetic.

1. Select a case and compare website evidence with related accounts.
2. Read every proposed field and operation.
3. Check the review confirmation, then **Approve and apply locally**, or **Reject proposal**.
4. Inspect the persistent synthetic accounts in **Local CRM**.
5. Refresh. Decided items do not return to the pending queue.

For an ambiguous relationship, prepare an explicit identity resolution first.
Preparation is not approval. Unselected accounts remain unchanged.

```bash
.venv/bin/python -m bellhaven.cli demo
.venv/bin/python -m bellhaven.cli audit
.venv/bin/python -m unittest discover -s tests -v
```

The ignored `data/demo.sqlite` file preserves snapshots, decisions, identity links,
and per-step execution outcomes across restarts. Demo and live data cannot share
a database scope. Do not delete the database between daily runs.

## Matching and business events

The system keeps website observations separate from CRM accounting records.
It compares the complete account population, not just names containing Bellhaven.
Address normalization preserves unit identifiers; names, care offerings, and
locality provide additional evidence. Common brand/service words do not dominate
fuzzy name comparisons. Exact-address candidates take precedence over weak name
similarities, but never bypass human approval.

Candidate relationship groups expose co-location, splitting, consolidation,
rebranding, and historical-account ambiguities. These are possible explanations,
not asserted historical events. Parent-company restructuring does not necessarily
mean a physical facility split. Several accounts at an address are not
automatically duplicates. Existing CHOW/duplicate links are retained as context.

The website collector follows public homepage, directory-pagination, about, and
community-detail links. A homepage-only community must not be missed merely
because it is absent from the directory. All declared directory pages, links,
and required detail fields must be accounted for. An incomplete crawl cannot
produce proposals, especially claims that a facility disappeared.

An unlisted account can receive a proposed `Needs Review` status and explanatory
note. Absence alone never establishes a sale, closure, or new parent.
There is no merge or delete operation.

## Billing protection

When a parent change is needed:

| Revenue history | Positive outstanding AR | Plan |
|---|---|---|
| No | No | Re-parent the existing account |
| Yes | No | Re-parent the existing account |
| No | Yes | Re-parent; flag the unusual combination for review |
| Yes | Yes | Create a current account; set only `chow_current_account` on the old account |

Missing/invalid financial values are not zero. Direct re-parenting does not
clear balances or change account IDs. CHOW never copies financial history into
the new account or edits the old account's name, parent, status, or note.
Server-managed timestamps are excluded from business-field comparisons.

Duplicate proposals choose a survivor and set `duplicate_of_account` plus
`Inactive` on losing copies. Automatic proposal construction is conservative:
identity evidence must agree, and losing copies must have zero revenue and AR.
Other cases remain explicit relationship reviews, not silent merges.

## Approval, audit, and recovery

- Approval is bound to the exact plan hash and account snapshot.
- Rejection is also persisted; timestamps do not create new proposal identities.
- Decided plans are not silently replaced by fresh evidence. Reopening is explicit,
  retains prior decisions, and is blocked when writes need reconciliation.
- Approval and successful application are separate states.
- A local file lock serializes executors. Each operation is recorded before writing.
- Relevant current account values are compared before additional writes.
- A new account ID is persisted before its dependent CHOW pointer update.
- A lost create response is checked read-only using its approved unique note marker
  and payload. The system never blindly repeats an uncertain POST.
- Unexpected changes, failed readbacks, and ambiguous outcomes stay visible.

## Live integration boundary

The public API inspection established `account_id`, `billing_*` address fields,
financial fields, and the `data/page/page_size/total` account-list envelope.
The OpenAPI specification does not define create/update body schemas.
**Actual create/update behavior has not been tested or exercised.**

The local UI deliberately has no live-connect button or token input in this build.
The live adapter defaults to locked/read-only, and the executor refuses live writes.
Do not unlock or run live collection until the owner confirms the next stage.
After confirmation, the read-only CLI entry point is:

```bash
# Example only: do not run before the owner confirms.
.venv/bin/python -m bellhaven.cli sync \
  --token-file /absolute/private/path/to/token \
  --parent-id VERIFIED_PARENT_ACCOUNT_ID \
  --owner-confirmed-live-test
```

The token file must be owned by the current user, mode `0600`, and not a symlink.
Never put the token in command arguments, code, GitHub, screenshots, logs, or
`.env.example`. The live client only targets the assessment API, refuses redirects,
and does not automatically retry writes. Real writes require a separately
confirmed live-review implementation and approval of each displayed proposal.

`examples/daily.cron` is an uninstalled future read-only schedule template, not an
enabled job. No deployment, hosting, LLM API key, or external data service is needed.

## Submission still requires real work

After the owner approves live integration, verify the API contract, review and
apply justified corrections to the isolated CRM, and record about two minutes
of real review: evidence, one approval, one rejection, and the approved result in
the CRM browser. Hide the browser address bar because its URL contains a token.

Submit the repository/shared archive, viewable video link, honest actual time,
and the completed writeup. This repository does not invent time spent or claim
the real CRM has been corrected.
