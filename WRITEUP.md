# Bellhaven CRM reconciliation: writeup

**Result.** 35 website communities, 121 CRM accounts, 25 cases: 24 approved and
applied, 1 rejected. A fresh rerun proposed nothing new, and the CRM differs from
its first snapshot only by the approved changes.

## Matching approach

I compare every community with all 121 accounts, not just Bellhaven-named ones,
because acquisitions and rebrands change names and parents. The address is the
identity: a normalized street plus the ZIP or city and state. Names only support a
match, so look-alikes elsewhere are shown as evidence, never matched.

- **SOP.** If revenue and AR are both above zero, the old account keeps everything
  except `chow_current_account`, which points to a new Bellhaven account (Tiffin,
  Marietta). Otherwise I re-parent directly.
- **Duplicates.** I keep the copy with the most AR and revenue; the others get
  `duplicate_of_account`, Inactive, and a note.
- **Gone from the website** is not proof of a sale, so those accounts get Needs
  Review and a note, unless exactly one other operator's account has the same
  address (Sandusky now points to it).
- **Union Square, rejected.** A same-ZIP look-alike exists under another operator,
  so no new account until a duplicate is ruled out.
- Phones are not reconciled; they are outside the requested fields.

Nothing writes without approval, every write is read back, each refresh verifies
its reads a second way, and decided cases are never re-proposed.
`examples/daily.cron` runs the pipeline daily and only proposes.

## How I used AI tools

GitHub Copilot, as a coding agent, wrote nearly all the code and tests. I directed
and checked it: synthetic data first, then read-only against the sandbox, writes
last. I asked for an independent check of every read and had it proven by
tampering with reads. I reviewed every case and clicked every decision myself.
The pipeline makes no LLM calls.

## What I'd build next

- Outside sources for what the website alone can't settle: state licensing and
  CMS ownership records to confirm sales, closures, and look-alikes like Union
  Square, and a trusted listing for phone numbers.
- A live schedule with alerts.
- API idempotency keys, so lost writes can be retried safely.
