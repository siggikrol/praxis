# Networking content maintenance

The existing Readiness Gate → Review → Edit/Save/Refresh → ServiceNow workflow uses
this content bundle. No UI editor or second ServiceNow client is introduced.

The Single-VPC networking contract has since been corrected explicitly; see the
[source trace and scope](../../../../docs/single-vpc-networking-corrections.md).
`presentations/networking-single-vpc.yaml` inherits the common presentation and
overrides sections/field wording without duplicating identity fields. Its profile
separates service DNS from ingress, supplies source attribution, and records
unconfirmed service mappings. Multi-VPC and legacy NCR Builder remain on their
previous contract pending review. The preservation notes below describe the initial
externalization; the correction trace records the intentional Single-VPC changes.

Single-VPC normal rendering omits provenance and component IDs. For diagnostics,
pass `include_details=True` to `build_connectivity_manifest` or
`build_request_template`, or expand “Show generated provenance and component
details” on the review page. That panel is separate from the editable ticket and
is not included by Copy template or normal submission. Known RabbitMQ and Vault
DNS destinations are rendered with their service ports; only their source mappings
remain unresolved. TCP/5552 purpose/source/destination remain unresolved.

## Files and ownership

- `profiles/platform-common.yaml`: existing infrastructure defaults, routing,
  conditional requirements, and detailed NCR Builder options shared with RGS/Loyalty.
- `profiles/catalyst-common.yaml`: Catalyst endpoint pack and additional egress.
- `profiles/catalyst-multi-vpc.yaml`: four existing VPC allocations.
- `profiles/catalyst-single-vpc.yaml`: one existing allocation.
- `profiles/rgs.yaml`, `profiles/loyalty.yaml`: existing non-Catalyst endpoint packs.
- `presentations/networking.yaml`: editable SNOW field definitions and Jinja section
  templates. Technical values come from the resolved profile and readiness draft.
- `presentations/ncr-builder.yaml`: presentation of existing detailed NCR options.

Common rules are inherited, not copied into architecture profiles. NCR Builder
uses the shared endpoint pack, service/allow-IP defaults, and externalized legacy
options. Its optional detailed egress summary retains its previous granularity;
Readiness Gate does not automatically select additional NCR Builder options.

## Schema and rendering

Each YAML file requires `schema_version: 1`. Profiles optionally `extends` one
profile by filename stem. Mapping keys merge recursively; lists/scalars replace.
Order is significant and is included in revision hashes. Duplicate YAML keys,
unknown profile/rule properties, malformed records, inheritance cycles, and
invalid conditions are rejected. There is no silent fallback to bundled content
when an external bundle is configured but invalid/missing.

`endpoints`, `egress`, `routing`, `conditional`, and `vpcs` are mappings with stable
record IDs. Remove a common record at its source to remove it from both Catalyst
architectures. Architecture-specific overrides only belong in that architecture.
Conditions use `condition: {field: grafana_required, equals: 'yes'}`; they do not
execute Python. A condition must reference data actually supplied by readiness.

Endpoint records contain `namespace`, `name`, `gateways`, `host`, and `protocol`.
Existing `{ENV}`/`{DOMAIN}` host tokens remain compatible with NCR Builder.
Egress records contain `destination`, `protocol`, and `purpose`, and may specify
`source` (default: allocated VPC CIDRs or the existing placeholder). Text values
are rendered with Jinja. Routing/conditional records contain `requirement`.
VPC records use `cidr_field` to reference an allocated readiness value; optional
`requested_prefix` is an integer 0–32. No new prefix defaults are supplied here.

The presentation uses strict, sandboxed Jinja with a plain data context, not Flask
globals. Available context includes `data`, `profile`, `defaults`, `vpcs`,
`endpoints`, `egress`, `routing`, `conditional`, `environment`, `domain`, `region`,
and `rancher`. `sections` are rendered in file order. `manifest` is the complete
connectivity text; request field values also receive `draft_url` and `manifest`.
Missing template variables cause a clear error instead of silently omitting data.

Presentation fields require unique `key`, `label`, `value` (Jinja), and `type`
(`text`, `textarea`, `date`, `url`). Every displayed field is editable and required
for OAuth submission. Optional fields can use a data condition. Keep short
description/requested-for and existing identity fields unless the catalog contract
explicitly requires a different presentation.

Sections are provided for CIDRs, routing, DNS, ingress, cross-VPC, egress and
shared services. Cross-VPC content is initially empty: no exact flows are invented.
To split a large manifest, replace its presentation field with fields whose values
render the relevant sections, then configure confirmed remote mappings and limits
for all resulting fields. Preserve all requirements when splitting; split mode
validates mapping coverage, not the semantic completeness of a custom template.

## Runtime updates

Set `PS_NETWORKING_CONTENT_DIR` to a complete bundle with `profiles/` and
`presentations/`. The default is this directory. In Docker, for example:

```yaml
services:
  web:
    environment:
      PS_NETWORKING_CONTENT_DIR: /etc/praxis/networking
    volumes:
      - ./networking-content:/etc/praxis/networking:ro
```

Deploy the mount/configuration once; subsequent content edits need no Python
changes, image rebuild or process restart. Content is read afresh on use, in every
worker. Publish a validated bundle atomically where possible. Changing only files
baked into an image still requires replacing the image. Environment-variable
configuration changes generally require recreating the process/container.

Resolved profile and presentation SHA-256 hashes are stored with reviewed fields.
The existing Harbor environment override is included in profile versioning.
On a subsequent review visit, a changed bundle marks the saved request stale and
preserves its entire reviewed definition, including fields removed from the new
template. A stale browser POST cannot save/submit old content as the new version.
Use Refresh, review the new values, then Save/Submit. Refresh uses current readiness
data plus current content. Legacy saved templates without hashes require refresh.

The submission ledger retains an immutable `snapshot_json`, including content
hashes, from the submission attempt. Manual attachment also records a snapshot.
Refreshing an attached request only changes the local working review. There is
no API to update an already-created ServiceNow request. Pre-existing submissions
cannot acquire a historical snapshot retroactively. A pending/unknown outcome
still blocks retry until the existing request is reconciled.

## ServiceNow configuration that must be confirmed

The repository does not establish the real catalog variable for a complete
networking body, per-section variables, or their character limits. No new remote
variable names have been invented.

- `SERVICENOW_NCR_VARIABLE_MAP`: JSON object **remote catalog variable → local
  field key**. It now extends existing defaults. The local key `request_content`
  contains the entire reviewed request, including the multi-VPC allocation field.
- `SERVICENOW_NCR_CONTENT_MODE=complete` (default): a confirmed variable must map
  to `request_content`. This closes the missing `requested_vpc_count` and full-body
  mapping gaps without assuming a remote `description` variable exists.
- `SERVICENOW_NCR_CONTENT_MODE=fields`: every reviewed field must be mapped,
  including `requested_vpc_count` when present and any split section fields.
- `SERVICENOW_NCR_FIELD_LIMITS`: JSON object **remote catalog variable → positive
  character limit**. Every nonempty outgoing NCR variable needs a confirmed limit,
  including legacy default variables. No arbitrary ServiceNow limit is assumed.
- `SERVICENOW_AWS_FIELD_LIMITS`: optional equivalent limits for AWS requests.

Unknown mappings/limits and oversized values fail before OAuth or ordering, with
the affected local/remote field and measured/supported lengths. Nothing is silently
truncated. The old 5,000-character review limit is removed. Web-server request-body
limits still apply independently. Custom mappings may remap any local field to
another remote variable; the pre-existing default mappings remain active.
`requester` and `requested_for` API aliases both use the reviewed requested-for
value. The original readiness owner/requester identity is not rewritten.

Instance, OAuth credentials, catalog IDs, assignment/routing identifiers, variable
names, and remote field limits are deployment configuration, not template wording.
Confirm the existing default catalog names too before enabling production OAuth.
Manual copy/attachment remains available without this integration configuration.

## Preserved behavior and unresolved decisions

- Multi-VPC still requires ILP, CGS, PMV and CR. Optional PMV/CR is NOT enabled.
- No `/21` or `/22` defaults were introduced; the existing requested size remains.
- Existing generic egress destinations, exposure detection, and ports are preserved.
  No new Docker/package-registry destinations or RabbitMQ 5672 rule was added.
- The profile's `requires_confirmation` items are visible on the review page.
- The readiness validation/UI and provisioning handoff still have the existing
  fixed four-VPC structural contract. Optional deployments require coordinated
  changes there; profile conditions alone must not be used to change that contract.
- Generic readiness verification check definitions remain Python application
  workflow. Networking endpoint/rule/default lists and request wording are external.
- NCR Builder retains legacy field identifiers such as `port_8080_cross_vpc`;
  labels/defaults and emitted requirement text are external. Its document shell
  headings/table formatting remain Python. AWS account request wording is unchanged.

## Validation

Run `python -m pytest tests/test_networking_content.py tests/test_ncr_ingress.py
tests/test_environment_readiness.py -q` (on one line). Regression fixtures contain
hashes of the pre-refactor networking/SNOW and NCR Builder outputs; intentional
future contract changes must explicitly update these baselines.
