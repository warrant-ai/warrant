# Security

Warrant exists so that a third party can verify a record without trusting the system that wrote it. A flaw in that promise is the most serious kind of bug this project can have.

## Reporting

Report privately through GitHub: https://github.com/warrant-ai/warrant/security/advisories/new. Do not open a public issue for anything that could let a record be forged, altered, or made to verify when it should not.

You will get an acknowledgement within five business days. If the report is confirmed, you will be told what the fix is and when it ships, and credited in the changelog unless you ask otherwise.

## What counts

- A record or export that verifies after its body, its position in the chain, or its signature was altered.
- A signature accepted from a key outside its `not_before` or after its `revoked_at`.
- A difference between the Python and JavaScript canonical forms, hashes or signatures that lets a record verify in one and not the other, or lets two different bodies produce one hash.
- A checkpoint a witness co-signs that is not consistent with the one it signed before.
- A policy evaluation that returns `allow` where the policy says `deny`, or that applies a fail mode where no clause failed to evaluate.
- Content a record should carry only by digest reaching the ledger in the clear, including through an exception message or a redaction gap.
- A collector request that reads or writes another tenant's records.

## What is out of scope

- The demo keys in `examples/` and the signing seed used by the public site's verifier. They are published on purpose and labelled as such.
- The fact that an unsigned hash chain does not prove its operator did not re-seal it. The specification and the evidence pack state this; signing and witnessed checkpoints are the answer, not a fix to the chain.
- Vulnerabilities in dependencies that do not reach the behaviours above. Report those upstream.

## Supported versions

Fixes ship in the next release on the current minor line. Older releases are not patched; `CHANGELOG.md` says which releases are affected by a fix.
