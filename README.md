# Bellhaven Review Workbench

A local, approval-first workbench for reconciling facility identities and current
parent relationships without destroying billing history.

By default the app runs an offline demo on synthetic data and never loads a
credential. [Live mode](#live-mode) works against the assessment sandbox and is
read-only unless writes are separately enabled. The sandbox review is complete;
[WRITEUP.md](WRITEUP.md) covers the approach, decisions, and results.

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
change of ownership (CHOW), a missing account, exact-name and name-variant duplicates, a current account
whose former billing account lacks its change-of-ownership link, a stale care type, a ZIP typo on
an Inactive but listed facility, a shared-campus/split ambiguity, an existing
historical change of ownership, missing financial data, and an unlisted account.
All facilities, addresses, balances, and source references in the demo are synthetic.

1. Check **Last refreshed** under the title (the same line says whether read
   verification passed), then open a case in **Pending cases**. A case always opens
   at the top of the page; edits inside it keep your scroll position.
   **Facilities overview** is a separate main view, not a case. It lists every
   website facility with its website address and care offerings, current CRM
   account, parent and status, match result, linked case, and review status.
   A second table lists Active or Needs Review Bellhaven accounts that no website
   facility matched. Select a row to open its case; the case's back button returns
   to the overview.
2. Read **Evidence**. All website facilities and related CRM accounts appear together
   in the aligned comparison, not behind record selectors. CRM values that differ from
   the website (by the same rules the matcher uses) are highlighted. Phone is not
   reconciled, so it is left out of the comparison. Financial details remain CRM-only.
3. Review **Change** below the evidence. **Recommended change** opens directly into
   account forms, without a separate change table or reason summary.
   Edit proposed values on the same page; changed values automatically mark
   the plan **Custom change**. Restoring the recommended values restores the recommendation.
   Required change-of-ownership and duplicate links remain visible as read-only account fields.
   Each account form explains the actual write and which fields stay unchanged;
   this explanation follows inline edits.
   **Decision reason** is prefilled with the recommendation rationale and remains editable. Approval requires
   a nonempty reason and confirmation of the displayed plan. Rejection requires its own
   **Rejection reason**; the recommendation rationale is never recorded as a rejection reason.
   Editing alone never writes CRM account fields. Then approve locally or reject the proposal.
4. Inspect the case's activity log and the synthetic **Local CRM** in the sidebar.
5. Refresh. Decided items do not return to the pending queue.

**History** contains only cases with an approval or rejection decision. Automatic
proposal refreshes and unreviewed drafts do not enter History. Open a case to see
approved CRM changes and review decisions separately; local proposal maintenance
is retained in a clearly labeled **Local proposal activity - no CRM writes** section.
Local refreshes never count as CRM writes.

Open any reviewed case to see who acted, when, the reason, and verified before/after field values.
Set **Reviewer name** in the sidebar before changing records. It is a local audit
label, not an authenticated identity. Legacy records without actor information
are shown as **Not recorded**, never assigned to the current reviewer.

For an ambiguous relationship, the issue names the actual conflicting facilities
and accounts. Approving the recommendation flags the Bellhaven account(s) Needs Review
with a note; it does not settle the account mapping, and accounts under other
operators are left unchanged. Every pending case offers **Prepare a different
correction** (expanded for ambiguous mappings):

- **Map a website facility to an account**, or create a separate current account.
- **Mark an account as a duplicate** of a surviving account (Inactive plus
  `duplicate_of_account`).
- **Link a historical billing account (change of ownership)** to an existing current account under
  a different parent. Only accounts with revenue history and outstanding AR above
  zero are eligible; only `chow_current_account` changes.

Preparation opens a separate proposal for approval and survives refreshes;
unselected accounts and other mappings are not changed.

```bash
.venv/bin/python -m bellhaven.cli demo
.venv/bin/python -m bellhaven.cli audit
.venv/bin/python -m unittest discover -s tests -v
```

The ignored `data/demo.sqlite` file preserves snapshots, decisions, identity links,
and per-step execution outcomes across restarts. Demo and live data cannot share
a database scope. Do not delete the database between daily runs.
Case IDs combine the original UTC creation date/time and permanent sequence,
for example `CASE-20261119T003400Z-000001`. They are workspace-unique and also
appear in decision history and audit exports. Filtering, refreshing, approving,
rejecting, and reopening do not change a case's creation time or sequence.
Each case has a specific one-sentence issue summary. Website/CRM comparisons
contain comparable source fields; revenue, AR, and account status appear only
in the separate CRM account details section.

## Matching and business events

The system keeps website observations separate from CRM accounting records.
It compares the complete account population, not just names containing Bellhaven.
Address normalization preserves unit identifiers; names, care offerings, and
locality provide additional evidence. Common brand/service words and legal suffixes
(LLC, Inc, Ltd) do not dominate fuzzy name comparisons.

An account is a confident identity candidate when:

- its street matches and either the ZIP or the city and state match (a ZIP typo
  still matches), unless care offerings conflict *and* the names differ, which
  suggests a co-located facility and needs review;
- its exact name appears in the same city and state with a different street
  (an outdated address); or
- its exact name matches and the CRM account has no address.

Street comparison treats common abbreviations as equal (Street/St,
Northwest/NW, Pike/Pk, and similar).

A confident match proposes every stale field the website shows: name, street,
city, state, ZIP, care type, parent, and Inactive status. CRM accounts hold one
primary care type, so a CRM value matching any website offering is consistent.
Corrections and new accounts use CRM vocabulary (Assisted Living, Memory Care,
Skilled Nursing, Independent Living) taken from the first website offering.
A PO Box billing street is kept: it is a legitimate mailing/billing address,
not a stale facility address. Similar names at
other addresses are shown as related evidence only; they do not block creating
the missing account, and such an unmatched Bellhaven account is checked as unlisted.

When several confident accounts represent one website facility, the system keeps
one current account: billing-protected history last, then the most outstanding AR,
revenue history, the Bellhaven parent, Active status, and an exact name match.
Other copies become Inactive duplicates. A copy under another parent with revenue
history and outstanding AR is instead treated as the former operator's billing
account: it receives only `chow_current_account`, pointing to the existing current
account rather than a second new one.

Candidate relationship groups expose co-location, splitting, consolidation,
rebranding, and historical-account ambiguities. These are possible explanations,
not asserted historical events. Parent-company restructuring does not necessarily
mean a physical facility split. Several website facilities sharing one account
stay as an account-mapping review. Existing change-of-ownership and duplicate links are retained as context.

The website collector follows public homepage, directory-pagination, about, and
community-detail links. A homepage-only community must not be missed merely
because it is absent from the directory. All declared directory pages, links,
and required detail fields must be accounted for. If any page fails, cases are
still generated for the parsed locations, the Pending view shows the crawl errors,
and no account is claimed to be missing from the website.

Status follows website evidence, and every status change still needs approval:

- **Active**: a confidently matched facility the website currently lists, including
  an account that was Inactive in CRM.
- **Inactive**: a duplicate copy linked with `duplicate_of_account`.
- **Needs Review**: a Bellhaven account the complete crawl no longer lists, an
  ambiguous account mapping, or an account whose financial data blocks the SOP.
  Absence does not establish a sale, closure, or new parent, so the note explains
  what needs review while the parent and balances stay unchanged. Same-address
  accounts under other parents are listed in the note and evidence.

A Bellhaven account the website no longer lists is treated as sold when exactly
one current account under another parent company has the same address. If the
old account has revenue history and outstanding AR, it stays exactly as it is and
only `chow_current_account` points to that existing current account, rather than
creating a second account for the new owner.

Historical change-of-ownership accounts and accounts under other operators are not flagged by
review recommendations. A reviewer can change any recommended status inline,
which records a custom change and requires a reason. When a step leaves status
unchanged, the field is labelled **Account status (optional)**. There is no merge or delete operation.

## Read verification on every refresh

Every pipeline run (sidebar refresh, the re-check after each approved write, and the
daily command) verifies its own reads before anyone approves from them. Each check
re-derives a fact in a different way from the main pipeline:

| Area | Check | Severity if it fails |
|---|---|---|
| Website | A second crawler follows every internal link and must find exactly the scraped community pages | FAIL |
| Website | The directory's declared page and community totals match the pages and cards read | FAIL |
| Website | A regex parser re-reads each community page; name, street, city, state, ZIP, care offerings, and phone must equal the scraped values | FAIL |
| Website | Directory cards (name, city, care offerings) agree with the community pages | FAIL |
| Website | The main crawl finished without errors | WARN |
| CRM | A server-side `parent_id` filter returns exactly the Bellhaven accounts found in the full read | FAIL |
| CRM | Every account read individually (`GET /accounts/{id}`) equals the full list read | FAIL |
| Matching | Every website facility is classified exactly once | FAIL |
| Matching | Every current Bellhaven account is matched or flagged as not on the website | FAIL |
| Matching | A looser address scan (house number, street word, ZIP or city) finds no account left out of matching | WARN |

Same-name accounts at other addresses and communities outside the directory are listed
for information. Results are stored with each snapshot.

- **PASS**: the line under the title reads `Last refreshed: ... | Read verification passed`.
- **FAIL**: a red alert lists the failed checks on every page, and approval is blocked
  until a refresh passes. Unverified snapshots also block approval.
- **WARN**: a yellow alert lists the warnings; approval stays available.
- The sidebar's **Read verification** panel shows every check, its detail, and when the
  website pages were last verified.
- `bellhaven.cli sync` prints the problems and exits with code 2 on a failure, so a
  scheduler can alert on it.

After an approved write, the website crawl is reused, so its website checks are carried
over with their original time; the CRM and matching checks run again. A full refresh takes
about half a minute because it reads every page and every account twice.

The verification tests tamper with otherwise clean reads (a changed ZIP, a dropped care
offering, a missing homepage-only community, a misread or missing CRM account, a deleted
classification) and require each tampering to be caught.

## Billing protection

When a parent change is needed:

| Revenue history | Positive outstanding AR | Plan |
|---|---|---|
| No | No | Re-parent the existing account |
| Yes | No | Re-parent the existing account |
| No | Yes | Re-parent; flag the unusual combination for review |
| Yes | Yes | Create a current account; set only `chow_current_account` on the old account |

Missing/invalid financial values are not zero. Direct re-parenting does not
clear balances or change account IDs. A change of ownership never copies financial history into
the new account or edits the old account's name, parent, status, or note.
Server-managed timestamps are excluded from business-field comparisons.

Duplicate proposals keep one current account and set `duplicate_of_account` plus
`Inactive` on the other copies; nothing is merged or deleted. A copy with billing
history is flagged with a warning before approval. When a current account already
exists, a former operator's billing-protected account is linked to it with
`chow_current_account` instead of creating another current account.

## Approval, audit, and recovery

- Approval is bound to the exact plan hash and account snapshot.
- Rejection is also persisted; timestamps do not create new proposal identities.
- Decided plans are not silently replaced by fresh evidence. Reopening is explicit,
  retains prior decisions, and is blocked when writes need reconciliation.
- Approval and successful application are separate states.
- Custom actions retain the case ID, create an approved revision, and preserve the
  original issue summary and reviewer reason.
- Custom outcomes are not reversed on unchanged reruns; changed source evidence
  or CRM context can trigger a new review.
- A local file lock serializes executors. Each operation is recorded before writing.
- Relevant current account values are compared before additional writes.
- A new account ID is persisted before its dependent change-of-ownership pointer update.
- Write receipts retain the original initiating reviewer. If someone else verifies
  an interrupted write, the case distinguishes the writer from the verifier.
- A lost create response is checked read-only using its approved unique note marker
  and payload. The system never blindly repeats an uncertain POST.
- Write response bodies are undocumented, so a 2xx response only acknowledges the
  request: every write is confirmed by reading the account back, and a create whose
  response does not identify the account is found read-only by its note marker.
- Unexpected changes, failed readbacks, and ambiguous outcomes stay visible.

## Live integration boundary

The public API inspection established `account_id`, `billing_*` address fields,
financial fields, and the `data/page/page_size/total` account-list envelope.
The OpenAPI specification lists `POST /accounts` and `PATCH /accounts/{account_id}`
but defines neither their request nor their response bodies. Live writes returned
`account_id` and `message` for a create, and `account_id`, `fields`, and `message`
for a PATCH, never the account itself, so every write is confirmed by readback.

### Live mode

The workbench runs against the assessment sandbox when started with:

```bash
BELLHAVEN_MODE=live \
BELLHAVEN_TOKEN_FILE=/absolute/private/path/to/token \
BELLHAVEN_PARENT_ID=VERIFIED_PARENT_ACCOUNT_ID \
.venv/bin/streamlit run app.py --server.address 127.0.0.1 --server.port 8768
```

- **Read-only by default.** The sidebar refresh crawls the public website and
  reads every CRM account. Pages render from that snapshot without API calls.
  Approval is locked; rejecting or preparing a correction is local only.
- **Writes need a separate flag.** With `BELLHAVEN_LIVE_WRITES=1`, approving a
  case re-reads the affected accounts, stops if they changed since review, writes
  only the displayed plan through the API, and reads every write back.
- After an approved write, the app re-reconciles the last website crawl against a
  fresh CRM read. Use the sidebar refresh to crawl the website again.
- Live decisions use `data/live.sqlite` (ignored), separate from the demo database.

The equivalent read-only command-line collection is:

```bash
.venv/bin/python -m bellhaven.cli sync \
  --token-file /absolute/private/path/to/token \
  --parent-id VERIFIED_PARENT_ACCOUNT_ID \
  --owner-confirmed-live-test
```

The token file must be owned by the current user, mode `0600`, and not a symlink.
Never put the token in command arguments, code, GitHub, screenshots, logs, or
`.env.example`; the database stores only its SHA-256 fingerprint as a scope.
The live client only targets the assessment API, refuses redirects, and does not
automatically retry writes. Every live write needs an approval of the displayed plan.

`examples/daily.cron` is an uninstalled future read-only schedule template, not an
enabled job. No deployment, hosting, LLM API key, or external data service is needed.

## Live review results

The live run read 35 website communities and 121 CRM accounts and produced 25 cases:
24 approved and applied, 1 rejected. Every write was read back. A final full refresh
passed read verification and proposed nothing new, and a field-by-field comparison
with the first CRM snapshot found only the approved changes. See
[WRITEUP.md](WRITEUP.md) for the decisions behind them.

The CRM browser URL contains the token; keep the address bar out of screenshots
and recordings.
