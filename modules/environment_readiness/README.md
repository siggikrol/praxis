# Praxis Foundation Readiness

Praxis Foundation Readiness is a requester/architect-facing entry gate used before a DevOps
team starts provisioning an environment foundation with Praxis. Passing the gate means the
required account, networking, access, ownership, release, and platform prerequisites are available.
It does not mean the deployed environment or final product has been tested, accepted, or declared
production-ready.

The module uses a seven-step request-to-readiness flow. It captures:

- environment intent, requested region, naming, ownership, target date, and optional spec link;
- technical requirements needed to raise the AWS account service request and Networking/NCR request;
- the ServiceNow REQ/RITM references, followed later by the allocated account ID, CIDR, and firewall zone;
- verified foundation, networking, platform, access, artifact, and delivery prerequisites;
- conditional Grafana and jump-host requirements;
- risks, assumptions, and testing/sign-off ownership.

Users can save multiple incomplete environment drafts in the module's dedicated SQLite database (by default
`/var/lib/praxis-environment-readiness/environment-readiness.sqlite3`). Drafts are scoped to the authenticated
username, so they survive browser-session expiry and can be resumed from another browser after login.
That authenticated draft owner becomes the authoritative setup owner at publication; the editable requester
label is not used as an authorization decision.
Legacy drafts still present in a server-side session are migrated into SQLite on the next visit to the
draft list or workspace. The dedicated Entry Gate data volume must be retained. Markdown preview, clipboard copy, and `.md` download remain
locked until every required and applicable item passes server-side validation. Each active
prerequisite must be `Confirmed and verified` and must include an owner and evidence or ticket
reference. `In progress / ticket raised` and `Blocked` are useful draft-tracking states but do not unlock generation. Blank means not yet assessed; applicability is captured with Technical Requirements before requests are raised.

Networking and AWS verification links are signed, expire after seven days, and can update only the
requirement group named by the link. Set `ENVIRONMENT_READINESS_SHARE_MAX_AGE_SECONDS` to change the
expiry. Draft versions prevent an older browser tab or reviewer from silently overwriting a newer
save.

## ServiceNow integration

Automatic request creation uses the ServiceNow Service Catalog `order_now` API with an OAuth 2.0
client-credentials integration identity. Praxis never stores OAuth credentials in a draft or
published setup. Without the integration configuration, operators can still attach existing
REQ/RITM references and continue tracking the draft.

Configure these environment variables:

- `SERVICENOW_INSTANCE_URL` — for example `https://company.service-now.com`;
- `SERVICENOW_PORTAL_URL` — defaults to the configured instance URL;
- `SERVICENOW_CLIENT_ID` and `SERVICENOW_CLIENT_SECRET` — the inbound OAuth client credentials;
- `SERVICENOW_TOKEN_URL` — optional; defaults to `<instance>/oauth_token.do`;
- `SERVICENOW_AWS_CATALOG_ITEM_ID` — AWS account service catalog item `sys_id`;
- `SERVICENOW_NCR_CATALOG_ITEM_ID` — Networking/NCR catalog item `sys_id`;
- `SERVICENOW_AWS_VARIABLE_MAP` and `SERVICENOW_NCR_VARIABLE_MAP` — optional JSON objects mapping
  ServiceNow catalog-variable names to Praxis field names, extending the existing defaults.

NCR submission additionally requires confirmed full-content (or complete per-field) mappings and
catalog character limits. Networking profiles and Jinja presentations can be edited through a
mounted content directory without restarting the application. See the
[networking content maintenance guide](../ncr/content/README.md) for schema, reload/review behavior,
`SERVICENOW_NCR_CONTENT_MODE`, and `SERVICENOW_NCR_FIELD_LIMITS`. No remote variable names or
character limits are assumed for the new complete-content mapping.

Example variable mapping:

```json
{"u_customer":"customer","u_environment_name":"environment_name","u_region":"aws_region","u_jira_epic":"jira_epic"}
```

The real variable names must be taken from the two catalog items. Creating a request records its
number, URL, and submission state, but does not satisfy the readiness gate. The fulfilled account
and networking values must still be recorded and every applicable prerequisite confirmed.

Submission results are merged into the latest draft without replacing concurrent edits. Stale
review forms are rejected before an API call. If an order times out or returns an unreadable result,
the submission reservation remains pending because the remote outcome is unknown; repeated clicks
cannot create another order. Check the ServiceNow portal and attach the existing request reference
to reconcile it. Pending reservations also cover interrupted processes and are never automatically
expired. Completed submission references remain available on the review page for recovery.

Before OAuth is available, `Create in ServiceNow` opens an editable review page instead of failing
or inventing a request number. The operator can copy the prepared template, open the PRAXIS ServiceNow
portal, submit it manually, and attach the real REQ/RITM/SCTASK number and optional direct link.
Saving a template is explicitly recorded as not submitted. Once OAuth, catalog mappings and NCR
field limits are confirmed, the reviewed request can be submitted through the API. NCR preflight
requires either the complete reviewed body or mapping coverage for every reviewed field. A stale
networking profile/presentation requires Refresh and review before submission. Existing remote
requests are never updated by refreshing the local review.

Only the Networking/NCR request template includes the generated connectivity manifest sourced from
the canonical NCR platform profiles. It expands the selected Catalyst, RGS, or Loyalty profile into
environment-specific application hostnames and lists required EKS/VPC access to Harbor, regional
AWS/ECR services, the selected Rancher management plane, and shared Vault. Catalyst also includes
the canonical AD and CI/CD destinations. Every entry states source, destination, protocol/port, and
purpose. The manifest remains editable on the review page; the full NCR Builder is linked only for
requests that need detailed ingress/egress customization or attachments.

After validation, the operator publishes an immutable setup snapshot in Praxis. The
landing page lists these shared setup records, and each record has an authenticated URL for the
receiving DevOps owner. Publication enters an awaiting-acknowledgement state; the receiving user
must acknowledge the handoff before the `Mark setup started` action becomes available. Markdown copy and download remain
available as optional historical exports; the Studio record is the authoritative handoff.

## Ownership and permissions

Setup ownership and Receiving DevOps assignment are intentionally separate:

| Role | Allowed actions |
| --- | --- |
| Setup owner | Full control before completion: edit the source draft and setup definition, manage ServiceNow data, create scoped verification links, publish revisions, transfer setup ownership, reassign the Receiving DevOps owner, and perform all lifecycle and provisioning actions. After completion, the owner alone may reopen the setup as a new revision draft. |
| Receiving DevOps owner | Acknowledge the handoff, start provisioning, put the setup on hold, resume it, mark it complete, confirm bootstrap details, and start or continue the provisioning wizard. |
| Administrator | Perform either role before completion, including emergency ownership recovery and reassignment. Completed records remain read-only. |
| Verification-link holder | Update only the Networking or AWS/Platform prerequisite section authorized by the signed link. |
| Other authenticated user | View published setup records without changing them. |

The authoritative setup owner is the authenticated owner of the durable source draft, not editable
request text. When authentication is enabled, the Requester/owner field is tied to the signed-in identity.
The setup owner retains lifecycle control so they can acknowledge, start, hold, resume, or complete the
work when necessary. Owning a linked provisioning workspace alone does not grant setup lifecycle or
revision permissions.

The Receiving DevOps value must be the exact Praxis sign-in username or Azure AD
`preferred_username`, not only a team label. This exact match prevents similarly named users from gaining
operational permissions. Existing descriptive assignments such as `Fabio / Foundation Team` must be
replaced with the real authenticated identity through a setup revision. Configure a comma-separated
administrator list with `ENVIRONMENT_READINESS_ADMIN_USERS`; the compatibility default is `admin`.

To transfer the setup itself, the current setup owner opens the published record and selects
`Transfer setup ownership`. The operation atomically moves the durable source draft and ServiceNow state,
invalidates old verification links, updates the requester identity for the next revision, and records the
old owner, new owner, actor, and timestamp in ownership history. The previous owner immediately loses edit
and revision authority.

To change only the Receiving DevOps owner, the setup owner or an administrator opens the current published
record and selects `Reassign Receiving DevOps owner`. A reason and a different authenticated identity are
required. This is an operational handover, so it does not create a setup revision or change setup ownership.
The current lifecycle state is retained, the source draft is synchronized for future revisions, and a
dedicated audit entry records the previous owner, replacement owner, actor, timestamp, and reason. The
former Receiving DevOps identity immediately loses lifecycle access and the replacement identity receives
it. If provisioning has already started, ownership or editor access to the linked wizard workspace is handed
over as part of the same action so the replacement can continue the existing work.

Published records use the same dedicated Entry Gate SQLite database at
`PS_ENVIRONMENT_READINESS_DB` (default:
`/var/lib/praxis-environment-readiness/environment-readiness.sqlite3`). Docker Compose uses the
independent `praxis_environment_readiness_data` volume, and Kubernetes uses the independent
`praxis-environment-readiness-pvc`; neither is shared with Spec Workbench or general caches.

Environment names are unique logical setup identities. Publishing a name that already exists opens
a conflict screen instead of creating a duplicate row. Anyone can open the current setup or return to
their draft, but only the authoritative setup owner or an administrator can publish the validated snapshot
as the next immutable revision. Once a current setup exists, unrelated drafts for that environment name
are rejected during autosave and normal saves; the current source draft and an owner-authorized completed
setup reopen draft remain the only valid revision paths. Existing
duplicate rows are grouped into ordered revisions automatically; only the current revision appears
on the landing dashboard, while superseded revisions remain read-only in revision history.

The published-setup dashboard supports server-side search across environment name, customer, AWS
account ID, Jira epic, NCR ticket, setup owner, receiving DevOps owner, architect, and requester. Search terms
combine with customer and status filters, remain in pagination/status URLs, and scope the displayed
status counts. Completed setups remain outside the default Active queue.

Authenticated users also receive personal work queues for setups owned by them, assigned to them,
requested by them, or started by them. The authoritative Owned by me filter uses an exact login
identity; the reporting-only assigned/requested/started filters also support an Azure AD-style email local
part. Personal queues combine with every existing
status, customer, search, and pagination filter; team-only assignments remain in the All team view
unless the signed-in identity is explicitly included in the owner text.

Target dates drive operational urgency labels for due-soon, due-today, overdue, paused-overdue,
completed-on-time, and completed-late setups. The Active queue prioritizes overdue holds, other
overdue work, current holds, and imminent targets before normally scheduled work. Completion time
is persisted at the status transition. When a later published revision changes the target, the
detail view keeps both the original target and the current revision target visible.

The operational lifecycle is `Awaiting acknowledgement → Acknowledged → Started`, followed by On
hold/Resume or Completed. Acknowledgement records the authenticated user, timestamp, and optional
note in status history. Existing setups that had already started before this gate was introduced are
backfilled as acknowledged at their original start identity and time.

Completion is an immutable audit boundary. The published snapshot, its durable source draft, ownership,
Receiving DevOps assignment, verification links, lifecycle, bootstrap information, and wizard link can no
longer be changed or deleted by any role. Copy, download, history, and other read-only views remain
available. Only the authenticated setup owner can select `Reopen setup`; a reason and non-past target date
are required. Reopening clones the completed snapshot into a new draft with a new identifier and leaves the
completed revision untouched. Publishing that draft creates the next revision with a fresh `Awaiting
acknowledgement` lifecycle and no inherited completion timestamps, bootstrap confirmation, or provisioning
wizard. The Receiving DevOps owner must acknowledge and start the new revision again.

After setup starts, supported setup types expose a direct provisioning-wizard action. The setup type
selects the Catalyst single-VPC, Catalyst multi-VPC, RGS, or Loyalty wizard automatically. The first
launch creates one user-owned wizard workspace from the immutable published snapshot and preloads
the compatible environment, release, AWS, network, Rancher, EKS, Aurora, Redis, and Vault values.
Later visits continue that same workspace rather than creating duplicates.
Existing linked workspaces can explicitly refresh readiness-owned values without deleting wizard-only
choices. Catalyst multi-VPC readiness requires four separate, non-overlapping allocated CIDRs for
ILP, CGS, PMV, and CR; the generated NCR request asks Networking for all four allocations.

Lifecycle transitions retain operational context: acknowledgement and initial start notes are
optional, hold reasons are required, resume resolutions are required, and completion summaries are
required. Resuming an overdue setup also requires a non-past revised target date. The old and new
target dates are stored on the status event and shown in history, while the original published
target remains visible for audit.

The landing dashboard summarizes Awaiting acknowledgement, Acknowledged, Provisioning now, On
hold, Overdue, and Completed this month. Summary values respect the current customer, search, and
personal-work-queue scope. Every card links to its server-side filtered result; overdue and monthly
completion views use explicit URL parameters that remain active through search and pagination.

`Export filtered CSV` downloads every setup matching the current status, urgency, customer, search,
and personal-queue scope rather than only the visible page. The export includes identity and account
data, original/current targets, lifecycle timestamps and actors, days active, latest hold reason,
and current revision. UTF-8 output includes a stable dated filename, and formula-like user values
are neutralized to prevent spreadsheet injection.


## Readiness milestones and verification changes

The draft workspace shows two milestones: **Ready to raise requests** checks the environment,
request ownership, technical selections, and service applicability; **Ready to provision** also
requires allocated resources, every applicable prerequisite, technical attestations, and sign-off.
Request creation validates the first milestone on the server before opening the review flow.
The blocker summary groups unmet Networking and AWS/Platform checks with an owner, ticket/evidence,
and next action. It refreshes after autosave; the full gate validation remains available alongside it.

Each prerequisite requires a confirmed status, responsible owner and ticket/evidence reference.
Notes remain optional (required for a blocked status). There is no separate verification-result
field. The server records the signed-in verifier and UTC time when a complete row is confirmed.
Section-level owner and evidence are inherited by checks unless a row has an explicit override.
Verification uses checkboxes, with blockers and notes in expandable details. Confirm completed checks
saves partial progress and records section attestation only when all applicable checks are verified.
Confirm and continue performs the same confirmation without a second attestation checkbox. Historical result text
is retained in saved records, but no longer blocks publication or appears as a separate column.

Changes to account, region, setup identity, allocated networking, technical selections, or applicable
services invalidate the affected group's attestation and return its confirmed rows to In progress.
Existing evidence remains visible for review, but the verifier/date are cleared until the
checks are confirmed again. Save configuration changes before reconfirming the affected section.
The dependency map is maintained in `workflow.py` and enforced on durable draft saves, including
scoped verification saves. Unrelated delivery notes preserve verification.

Configuration changes flag saved ServiceNow templates for review. The review screen compares saved
values with current draft values and provides an explicit refresh action. Refreshing a template does
not modify an already-submitted ServiceNow request; update that request through ServiceNow. Change
guidance also explains when to publish a revision and refresh an existing provisioning wizard.

## DevOps clarification before provisioning

Published records include a handoff checklist for account access, network allocations, release,
Rancher, risks, testing ownership, and the first provisioning action. Before provisioning starts,
an authorized operator can **Return for clarification** with a required reason. This clears any
previous acknowledgement and blocks acknowledgement/start while the request is unresolved.
The setup owner or administrator records a response, publishing a corrected revision first when
needed. DevOps must acknowledge again before starting. Clarification reasons, responses, actors,
and UTC timestamps are retained across revisions of the same setup. Superseded and completed
records cannot accept clarification mutations; work already started uses the existing hold flow.
