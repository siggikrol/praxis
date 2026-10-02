# Praxis

Praxis is starting fresh as a workspace for creating Terraform modules. The default Docker setup enables Terraform Module Builder and Modules & Stacks, alongside the core application and authentication.

## Start locally with Rancher Desktop

Enable Kubernetes and the **Moby (dockerd)** container engine in Rancher Desktop. Install the Python requirements in a virtual environment. Keep your login settings in `.env` as described below, then deploy:

```sh
python deploy/kubernetes/deploy.py --auth-from-compose
```

This builds both images in Rancher Desktop and deploys Praxis with a persistent volume. The script always names the `rancher-desktop` context; it does not change your current Kubernetes or Docker context. Open **http://praxis.localhost:8088** on this installation. The hostname routes through Traefik; its HTTP port depends on your Rancher Desktop configuration.

For subsequent code changes, run `python deploy/kubernetes/deploy.py` to rebuild and redeploy while retaining login settings and drafts. Terraform Module Builder and Modules & Stacks are enabled. No AWS or Google credentials are required.

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

## Shared appearance

`static/style.css` owns both themes, shared component styling, and content geometry. Change the tokens at the top (`--studio-content-max-width`, `--studio-content-gutter`, `--bg-*`, `--text-*`, `--brand-*`, `--accent-*`) to update the application consistently. Terraform and Readiness consume these tokens; keep module-specific layout separate from brand color decisions. The header and login share `templates/_brand_wordmark.html`, using the original transparent logo and a CSS mask to lighten only its lettering in dark mode.

## OpenTofu checks

Each saved draft has **Initialize & validate** and **Run mock test** actions. Both deployments build a runner using OpenTofu 1.12.0; Kubernetes starts a fresh Pod per run, while Compose uses a separate runner service. Initialization uses `tofu init -backend=false -input=false`; validation uses `tofu validate`. Mock testing generates a dedicated test with `command = plan` and mocked root providers, then runs only that test. Existing upstream test files are excluded. Supply JSON input values, optional mock data defaults, and expected outputs in the test panel. Empty expected outputs produce a planning smoke test. Complex examples may need realistic mocks; a passing mock does not establish real-world deployability.

The runner has no published port, credentials, app data, or Docker socket. Its filesystem is read-only except disposable temporary storage, with resource and command time limits. Downloads during init need public internet. Validation and mock subprocesses inherit a Linux seccomp filter that permits UNIX provider RPC sockets but denies Internet sockets. One job runs at a time. Temporary files are removed after each run; no state is retained. Results and settings stay with the saved draft revision, and deleting the draft deletes its results. Logs can include user-provided test values, so use sample values.

When running the web app outside Compose, configure `PRAXIS_TOFU_RUNNER_URL` to a trusted, separately isolated runner. Do not expose the runner port publicly.

## Git-backed Modules & Stacks

**Terraform → Modules & Stacks** adds a module catalog, independent customer /
environment / AWS account targets, and version-pinned stack configuration. Module
Builder remains the local authoring and sanity-test step before a module PR.

Enable both `terraform_module_builder,terraform_stacks`. The foundation catalog
uses `/tmp/praxis-cache/terraform-foundation.sqlite3` (override with
`PRAXIS_TERRAFORM_DB`), alongside the unchanged drafts database on the persistent
volume. No AWS connection is needed to register targets, sync public Git tags,
create stacks or export definitions. Public Git access requires internet.

The workspace downloads a GitHub workflow kit and can submit configuration PRs,
dispatch validation/plan/apply and refresh results when a GitHub App is connected.
Cloud execution is disabled by default. Configuration validation never reports a
cloud plan or deployment. GitHub Actions owns execution; the web app does not run
Terraform against AWS. Plan approval is tied to the commit and saved-plan checksum.

See [workflow setup and scope](modules/terraform_stacks/workflows/README.md) for
GitHub permissions, offline-first setup, state metadata and the later AWS setup.
No GitHub repositories or cloud resources are created during local deployment.


### Submit Builder drafts to GitHub

Use **Submit to Git** on a saved draft. The saved revision must have a passing
sanity check. Choose a module name and `owner/repository`, review the generic-code
confirmation, and submit. Praxis registers the module in Modules & Stacks and
links the PR. Existing repositories need an initial commit (e.g. a README).
Repository creation is optional and explicit, always private; its token requires
additional Administration permission. Merge/tag in GitHub and sync versions to
make a release selectable for stacks.

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
to manage its environments and accounts, then open an environment to manage its
stacks. Creating a stack there fixes the customer/environment context and offers
only that customer's accounts plus shared accounts. Shared modules and shared
accounts have separate catalog views. Existing object URLs and records are
preserved; this changes navigation, not Git definitions or state identities.

### Upload your own module

Module Builder → **Upload module** accepts a ZIP and creates an owned, editable
draft with an explicit cloud label. A containing folder is removed; nested modules
and text assets are preserved. Limits: 2.5 MB compressed, 2 MB text, 200 files.
Unsafe paths, links, binary files and invalid Terraform syntax are rejected. Git
metadata, workflows, local state/plans, .env and .tfvars files are excluded with
an import notice. Review the draft, run a sanity check, then Submit to Git using
the same publishing/catalog flow as generated wrappers. Upload does not execute
code or create repositories.
