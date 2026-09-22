# NCR

NCR module providing blueprint routes (from `plugin.py`) and templates/forms for NCR-specific workflows. Supporting code sits under `service/` and `forms/` to compose NCR configuration data.

## Networking template administration

Open `/ncr/templates` (also linked from the NCR ingress page). Catalyst Single-VPC, Catalyst Multi-VPC, RGS, and Loyalty each have separate
drafts and published snapshots. RGS and Loyalty start from their existing
requirements and shared ticket layout; publishing either preserves isolation
from the other templates. Edit requirements,
set example environment values, and update the ServiceNow preview. Save creates
an immutable revision; publish selects the saved revision used by Readiness Gate.
Restore creates a new draft from a historical revision and requires publishing
before it takes effect. Unsubmitted networking reviews automatically update to the current published
version when reopened; previous edits are retained in audit history. Submitted
requests retain their saved content. Wizard users never select a template URL.

With authentication enabled, access uses `ENVIRONMENT_READINESS_ADMIN_USERS`.
Draft changes and publishing require CSRF validation and reject stale revisions.
Storage uses `PS_NCR_TEMPLATE_DB`, or a sibling database beside
`PS_ENVIRONMENT_READINESS_DB`; the default is in the existing persistent readiness
volume. Bundled YAML remains the fallback until an architecture is published.
Published snapshots are independent: later bundled/shared YAML edits do not alter
a published version. Preview values are illustrative and do not update drafts.
