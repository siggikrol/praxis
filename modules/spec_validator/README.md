# Spec Workbench

Spec Workbench is the draft-based tool for building, reviewing, validating, and
publishing `spec.yaml` content without going through the full wizard flow.

## Quick Start

1. Open `Tools` and launch `Spec Workbench`.
2. Pick a wizard template:
   - `Loyalty Template`
   - `Single-VPC Template`
   - `Multi-VPC Template`
   - `RGS Template`
3. Complete the required fields under `Configure the Template`.
4. Click `Generate Preview`.
5. Review the generated YAML under `Review and Create`.
6. Click `Create Draft`.
7. In the Draft Editor:
   - use `Save & Check Syntax` for YAML structure
   - use `Save` to keep work in progress
   - use `Save & Run Full Validation` when the YAML is ready
   - use `Publish` only after a successful validation

## Template Options

The setup card is meant to pre-shape the starter spec before the draft opens.

- `Praxis Core Version`: writes `praxis_core_version` at the top of the template
- `Base Prefix`: fills shared naming such as repo, environment, and stack naming
- `Environment`: one or more target environments, such as `dev`, `test`, `sit`, or `prod`
- `Domain`: shared DNS / domain value
- `VPC Profile`: seeds region, TGW, VPCE service, and AZ values
- `Rancher Cluster`: seeds Rancher cluster name, region, and prefix
- `Rancher Context`: seeds SSA Rancher context, cloud integration, and worker pool
- `Release Merge`: optional deployment version block from the release archive

The page lists any required values that are still missing. When configuration is
complete, `Generate Preview` becomes available. Release Merge remains optional
and is collapsed until needed.

## Draft Editor

After opening a draft, the Draft Editor gives you four main things:

- `Needs Input`: groups placeholders and starter values by environment and area, such as
  `Naming`, `Spacelift`, `Rancher`, and `Networking`
- `Editor Highlights`: optionally highlights compute shapes, versions, and AWS infrastructure signals
- `Top-level keys`: lets you lock onto a section and jump directly to it
- `Technical Details`, snapshot comparison, and histories stay collapsed until needed

Each draft has one of two purposes:

- `Playground / learning` supports experimentation, manual-spec verification, section guidance,
  semantic highlighting, and full validation without requiring architecture metadata.
- `Environment prerequisite` represents an executable architecture proposal. Architect,
  customer, environments, purpose, decision status, and ticket are required before publication.

Drafts retain their initial YAML as a comparison baseline. `Snapshot Compare` shows how the
current spec differs from that starter or import, as well as validated and published revisions.
`Fork Saved Draft` creates a new POC while recording its source draft and preserving the source
YAML as the new comparison baseline.

The readiness ladder checks YAML syntax, top-level structure, `spec_type`, unresolved inputs,
Praxis Core validation, architecture context, and Confluence configuration. Confluence pages
include an Environment Prerequisite Report with architecture metadata, validation identity,
content hash, starter lineage, and unresolved-placeholder count.

The YAML editor progressively adds search, next/previous match navigation,
go-to-line, block indentation with `Tab` / `Shift+Tab`, and indentation-preserving
line breaks. If JavaScript enhancement is unavailable, the underlying textarea
continues to work normally.

Known `Needs Input` items also offer guided value editing. The workbench validates
common CIDRs, domains, versions, Transit Gateway IDs, endpoint service names, and
platform identifiers, previews the exact YAML line change, and applies only that
line. `Show in YAML` remains available for direct or unsupported edits.

Recommended flow:

1. Open the template from the setup card.
2. Clear the `Needs Input` list.
3. Run `Save & Check Syntax`.
4. Run `Save & Run Full Validation`.
5. Publish only after validation succeeds.

The validation screen leads with a running, passed, or failed summary. Successful
runs return to the draft after a short cancellable countdown. Kubernetes metadata,
events, full errors, and Core logs remain available under `Technical Details`.


## Notes

- Use the template setup to do the repetitive shaping first.
- Use the Draft Editor for exact YAML changes.
- Release Merge is optional.
- Publish should only happen from a successfully validated draft revision.
