# Praxis

Praxis is starting fresh as a workspace for creating Terraform modules. The default Docker setup enables Terraform Module Builder and Modules & Stacks, alongside the core application and authentication.

## Start locally with Rancher Desktop

Enable Kubernetes and the **Moby (dockerd)** container engine in Rancher Desktop. Install the Python requirements in a virtual environment, then start the complete development environment:

```sh
./scripts/dev-start
```

This builds both images in Rancher Desktop, creates the namespaces, local login secret and persistent volume, and deploys Praxis. It also keeps the test runner image loaded so checks do not fail after the unused-image cleanup runs. If `.env` does not exist, the script creates it with a random Flask secret and the default local login. The script always names the `rancher-desktop` context; it does not change your current Kubernetes or Docker context. Open **http://praxis.localhost:8088** on this installation. The hostname routes through Traefik; its HTTP port depends on your Rancher Desktop configuration.

For subsequent code changes, run `./scripts/dev-start` again to rebuild and redeploy while retaining login settings and drafts. Terraform Module Builder and Modules & Stacks are enabled. No AWS or Google credentials are required.

To preserve login settings from an existing Docker Desktop Compose installation during a one-time migration, start with `./scripts/dev-start --auth-from-compose`.

For a **one-time migration from Docker Desktop Compose**, with the source containers running and all tests finished:

```sh
python deploy/kubernetes/migrate_compose.py
```

The migration pauses the old proxy, makes a consistent SQLite backup in `.local-backups/`, refuses to overwrite a Kubernetes workspace that already contains drafts, and compares every migrated draft before stopping the old containers. The Docker data volume is retained. Sign in again at the new address using the same credentials. Backups contain draft contents; the directory is ignored by Git. Kubernetes drafts and sessions live in the `praxis-data` PVC. Do not delete the PVC or reset Rancher Desktop without backing it up.

Each OpenTofu check creates a Job in `praxis-runs`. Its Pod receives only a read-only snapshot, has no Kubernetes token or application volume, and uses disposable temporary storage. Network policy allows public HTTPS downloads and cluster DNS, while blocking private network destinations. Validation and mock testing additionally disable Internet sockets. Runs have resource limits and a ten-minute deadline, with no automatic retries. Praxis persists results before scheduling Job/Pod/snapshot cleanup after 60 seconds. Result collection resumes after a web Pod restart. Deleting a draft also schedules removal of its outstanding Job.

To inspect the local deployment:

```sh
kubectl --context rancher-desktop -n praxis get pods,pvc,ingress
kubectl --context rancher-desktop -n praxis-runs get jobs,pods
```

## Alternative: Docker Compose

Copy `.env.example` to `.env`, set a random `FLASK_SECRET` and optionally override `BASIC_PASSWORD`, and enable the authoring module:

```dotenv
ENABLED_MODULES=terraform_module_builder,terraform_stacks
```

Then run:

```sh
docker compose up --build -d
```

Open `http://localhost`. The default local login is `admin` / `change-me`; override it with `BASIC_USER` and `BASIC_PASSWORD` in `.env` for a shared deployment. The app requires no AWS credentials, GitHub organization, kubeconfig, Spacelift configuration, or ServiceNow access. Building the image downloads public packages and base images.

Compose retains only the shared cache/session volume. Existing Readiness volumes are not mounted or deleted. The local proxy serves HTTP; set `FLASK_SECURE_COOKIES=1` behind HTTPS.

## Optional modules

Existing modules remain in the repository but are disabled by default. Explicitly empty `ENABLED_MODULES` means **no optional modules**. Compose defaults to `terraform_module_builder,terraform_stacks` when the variable is absent; running Python directly with it absent loads no modules. A comma-separated list enables only those modules, for example:

```dotenv
ENABLED_MODULES=status
```

Restart with `docker compose up --build -d` after changing the configuration. `ENABLED_MODULES=*` explicitly enables all existing modules. Integration settings must be supplied separately for any legacy features you enable; they are no longer included in the default Compose environment. `docker-compose.aws.yml` remains an optional AWS configuration overlay and does not itself enable any modules.

The homepage and navigation show only enabled modules. Existing AWS forms, generation logic, and saved workspace code are retained for later reuse.

## Checks

```sh
python -m unittest discover -s tests -v
```

## Create a module

The header groups authoring under **Terraform**. Module Builder and Saved drafts share a workspace with AWS and Google Cloud provider selection. Provider selection also applies to search pagination. Direct module URLs identify their own provider.

Open **Terraform → Module Builder**, search the public Terraform Registry or paste an AWS or Google Cloud module URL, and choose a version.

- **Create wrapper:** expose inputs as variables, fix JSON values, or retain upstream defaults. Select outputs, then create your draft. Required upstream inputs cannot be omitted.
- **Start from example:** copy an example from the chosen release of a GitHub-hosted module. External local module references are rewritten to the pinned Registry version; copied local submodules remain local. Review relative file references, provider configuration, and additional resources. Binary files and symbolic links are skipped with warnings. Upstream license notices are retained.
- Use **Edit** to change files, **Rename** to change the draft/ZIP name, or **Delete** to review and confirm permanent removal. Renaming does not rewrite Terraform content. Deleting a stale revision requires reviewing the newer draft first. Edit the files, save, and download a ZIP. Downloads contain saved files and source provenance. Drafts belong to the signed-in account and persist in `praxis-data` on Kubernetes or `praxis_cache` on Compose (`module-builder.sqlite3`). Concurrent edits cannot silently overwrite one another.

Browsing/importing requires public Registry and GitHub internet access. Saved drafts and exports work offline. Imports are limited to 200 text files / 2 MB, from repositories with archives up to 12 MB compressed / 32 MB uncompressed. Non-GitHub modules can still be wrapped.

Saving checks HCL syntax only. The optional OpenTofu checks described below validate the saved configuration and can run mocked tests. Configure providers in the calling project and observe upstream Terraform requirements. Wrapper outputs are conservatively marked sensitive. Praxis does not deploy infrastructure or publish a private registry.

Module package names must identify the technology and purpose, for example `terraform_module_builder`. Avoid generic names such as `builder` or `manager`.

### Root modules and deployment wrappers

A saved root module can wrap a public upstream module while retaining the Praxis interface. Its draft page lists every root-level `module` call with the source, version and file where it was declared. After the exact root revision passes validation or a mock test, **Release** commits that snapshot to its Praxis-managed GitHub repository and creates the next semantic tag. **Create deployment wrapper** defaults to the latest immutable Praxis release and allows any earlier release to be selected. Praxis creates a separate draft that calls the tag-pinned Git source, carries forward all variable declarations and provider/Terraform configuration, and forwards every declared output. The new draft records its parent release, repository URL, commit, tag, draft, revision and upstream provenance so the layers remain visible.

For example, `pds_network` continues to call `terraform-aws-modules/vpc/aws` internally, while `pds_network_wrapper` calls `git::https://github.com/<praxis-owner>/<repository>.git?ref=v1.0.0` through a module block named `pds_network`. The deployment wrapper never replaces that Praxis root layer with the upstream module. If the root module has no release, wrapper creation is blocked until it is tested and released.

Each detected public Registry module call has an editable exact version. Updating it changes only that module block and saves a new draft revision. Releasing a newer organization root version marks its deployment wrappers as having an update available. Accepting that update writes the new tag pin as another wrapper revision and automatically starts the wrapper's previous validation or mock mode. Only a passing result for that exact wrapper revision enables its own release and Git tag.

## Shared appearance

`static/style.css` owns both themes, shared component styling, and content geometry. Change the tokens at the top (`--studio-content-max-width`, `--studio-content-gutter`, `--bg-*`, `--text-*`, `--brand-*`, `--accent-*`) to update the application consistently. Terraform and Readiness consume these tokens; keep module-specific layout separate from brand color decisions. The header and login share `templates/_brand_wordmark.html`, using the original transparent logo and a CSS mask to lighten only its lettering in dark mode.

## OpenTofu checks

Each saved draft has **Initialize & validate** and **Run mock test** actions. Both deployments build a runner using OpenTofu 1.12.0; Kubernetes starts a fresh Pod per run, while Compose uses a separate runner service. Initialization uses `tofu init -backend=false -input=false`; validation uses `tofu validate`. Mock testing generates a dedicated test with `command = plan` and mocked root providers, then runs only that test. Existing upstream test files are excluded. Supply JSON input values, optional mock data defaults, and expected outputs in the test panel. Empty expected outputs produce a planning smoke test. Complex examples may need realistic mocks; a passing mock does not establish real-world deployability.

**Generate mock setup** prepares those JSON fields before a test. It prefers literal input values from the bundled example that calls the module root, fills missing required inputs from their declared types and names, and adds defaults for referenced provider data-source attributes. Literal outputs become assertions; computed outputs stay empty for review. Generation does not execute Terraform or save a test result. Review the values, then run the mock test.

The runner has no published port, cloud credentials, application data, or Docker socket. Its filesystem is read-only except disposable temporary storage, with resource and command time limits. Downloads during init need internet access. When a deployment wrapper references a private Praxis-managed GitHub release, Praxis includes the signed-in user's saved GitHub credential in the one-run payload. Git can use it only for `github.com` during initialization; the credential helper and token file are deleted immediately afterward, and the credential is never stored in test settings or results. Validation and mock subprocesses inherit a Linux seccomp filter that permits UNIX provider RPC sockets but denies Internet sockets. One job runs at a time. Temporary files are removed after each run; no state is retained. Results and settings stay with the saved draft revision, and deleting the draft deletes its results. Logs can include user-provided test values, so use sample values.

When running the web app outside Compose, configure `PRAXIS_TOFU_RUNNER_URL` to a trusted, separately isolated runner. Do not expose the runner port publicly.

## Git-backed Modules & Stacks

**Terraform → Modules & Stacks** adds released modules, customer / environment /
cloud account targets, and version-pinned Stack configuration. Module
Builder remains the local authoring and sanity-test step before a module release.

Enable both `terraform_module_builder,terraform_stacks`. The foundation catalog
uses `/tmp/praxis-cache/terraform-foundation.sqlite3` (override with
`PRAXIS_TERRAFORM_DB`), alongside the unchanged drafts database on the persistent
volume. No AWS connection is needed to register targets, sync public Git tags,
create stacks or export definitions. Public Git access requires internet.

The first Stack model generates configuration for a future OpenTofu runtime. It
does not submit plans, run apply, create state, or connect to cloud accounts.
Older catalog-backed Stack records retain their existing GitHub workflow screens
for compatibility; released-wrapper Stacks cannot enter that workflow.

See [workflow setup and scope](modules/terraform_stacks/workflows/README.md) for
GitHub permissions, offline-first setup, state metadata and the later AWS setup.
No GitHub repositories or cloud resources are created during local deployment.


### Release Builder drafts to GitHub

Use **Release** on a saved draft after that exact revision passes validation or a
mock test. Choose `owner/repository` and the semantic change type. Praxis calculates
the next version from stable repository tags, commits the saved snapshot to the
default branch and tags that exact commit. Repository creation is optional and
explicit, always private; its token requires additional Administration permission.

Praxis stores the release version, tag, commit, upstream source and version, and
originating draft revision. An organization root release becomes selectable when
creating a deployment wrapper. Wrapper releases retain the selected root release
link and cannot use an unpublished draft. Releasing or updating a wrapper does not
plan or apply cloud infrastructure.

### Configure released-wrapper Stacks

A Stack selects one published deployment wrapper release and pins its semantic
version, Git tag, and commit. Praxis reads the wrapper variables from that immutable
release snapshot and builds the Stack input form. Optional inputs can continue to
use wrapper defaults. Each configured input is stored as either a literal value or
an output reference to another Stack in the same environment, account, and region.
References create explicit dependencies, and self-references and cycles are
rejected.

Stack records contain only context, release pins, input bindings, and dependency
IDs. Wrapper Terraform files remain in the release repository and are never copied
into per-customer configuration. The generated `stack.yaml` includes the exact
wrapper commit source and unresolved Stack output references for a later runtime;
it contains no credentials, backend, state, plan, or apply instruction.

When a higher semantic version is published for the same wrapper, Stack pages show
the current and available versions. Praxis does not update the Stack until the new
version is selected and the regenerated inputs pass validation.

**Terraform → GitHub settings** stores each user's default GitHub owner and stack
configuration repository. It supports a fine-grained personal access token or the
existing deployment-configured GitHub App. Tokens are encrypted in the persistent
foundation database using a key derived from `FLASK_SECRET`; keep that deployment
secret stable. The token is never rendered back to the browser. Blank leaves it
unchanged; Remove saved token deletes it. Test connection checks authentication;
repository authorization is verified on use. Deploy behind HTTPS for remote access.
Do not paste credentials in chat or commit them to Git. A Mac SSH key does not
provide GitHub API authentication to the web app.

For existing repositories, grant Contents and Pull requests read/write, with
Actions read/write for workflow features. Limit the token to the chosen resource
owner and repositories. See [GitHub's permission reference](https://docs.github.com/en/rest/authentication/permissions-required-for-fine-grained-personal-access-tokens).

AWS / Google Cloud badges derive from saved source metadata, including older drafts;
unknown sources are explicitly labeled rather than guessed from their names.


### Customer-first navigation

Modules & Stacks opens a searchable, paginated customer directory. Open a customer
to manage its environments, then open an environment to manage its cloud accounts
and Stacks. Creating a Stack there fixes the customer/environment context and
offers only accounts in that environment. Existing legacy object URLs and records
remain readable.

### Upload your own module

Module Builder → **Upload module** accepts a ZIP and creates an owned, editable
draft with an explicit cloud label. A containing folder is removed; nested modules
and text assets are preserved. Limits: 2.5 MB compressed, 2 MB text, 200 files.
Unsafe paths, links, binary files and invalid Terraform syntax are rejected. Git
metadata, workflows, local state/plans, .env and .tfvars files are excluded with
an import notice. Review the draft, run a sanity check, then release it through
the same versioned lifecycle as generated root modules. Upload does not execute
code or create repositories by itself.
