📘 Wizard Extension Modules (`wizard_ext/`)
===========================================

This directory contains **Wizard Extension Modules** --- modular components that inject **additional forms, validation logic, or configuration inputs** into Praxis's multi-step wizards (Multi-VPC, Single-VPC, RGS, Loyalty, etc.).

These modules **do not define blueprints**, **do not create standalone pages**, and **do not render any UI outside the wizard flow**.\
Their only responsibility is to provide **form classes, validation helpers, and spec-building logic** that are consumed by the Wizard Engine.

* * * * *

✅ What *belongs* in this folder
-------------------------------

A module in this folder should:

-   Provide one or more **WTForms classes** used inside wizard steps

-   Include **constants**, **service helpers**, and **validation rules**

-   Produce **spec fragments** that merge into the final deployment spec

-   Have **no standalone routes or UI navigation entry**

-   Be imported explicitly in wizard definitions, like:

`from modules.wizard_ext.rancher_helm_pipelines.forms import RancherHelmPipelinesForm

WIZARD_STEPS = [
    ("rancher_helm_pipelines", RancherHelmPipelinesForm),
]`

* * * * *

🚫 What *does NOT* belong in this folder
----------------------------------------

A module **should not** be placed here if it:

-   defines a **Flask Blueprint**

-   has its own **navigation entry** on the PS home page

-   exposes **user-facing pages**

-   represents a **standalone tool** (Releases Explorer, NCR, Admin tools, etc.)

Those belong under `modules/` in their own feature folders.

* * * * *

📁 Typical structure of a Wizard Extension Module
-------------------------------------------------

Each module should follow this pattern:

`my_feature/
  ├── constants.py         # deterministic allowed values, defaults
  ├── service.py           # sanitization, mapping, spec helpers
  ├── forms.py             # WTForms used by wizard steps
  └── __init__.py`

Optionally:

 `├── validators.py        # custom validators
  ├── schema.json          # future S3-backed config schema`

No `plugin.py` is used here because these modules are **not standalone plugins**.

* * * * *

🧩 How Wizard Extension Modules are used
----------------------------------------

Wizard extension modules are manually injected into a wizard's step list:

`WIZARD_STEPS = [
    ("project_settings", ProjectSettingsForm),
    ("helm_whitelist", HelmWhitelistForm.for_profile("multi-vpc")),
    ("rancher_helm_pipelines", RancherHelmPipelinesForm),
    ...
]`

This explicit ordering ensures:

-   deterministic wizard behavior

-   reproducible CI tests

-   no implicit or magical auto-discovery

-   full control over backward compatibility

* * * * *

🔧 Guidelines
-------------

### 1\. Keep all code deterministic

No dynamic imports, no filesystem scans, no auto-detection.

### 2\. No business logic inside the form

Put logic in `service.py`.

### 3\. Keep comments generic

No Praxis-specific business context.

### 4\. All constants must be pure data

No function calls or dynamic data at import time.

### 5\. No external network calls inside these modules

Wizard steps must be fast and deterministic.

* * * * *

🧱 Purpose of this folder
-------------------------

This folder isolates **wizard-level configuration logic** from:

-   standalone modules

-   UI modules

-   REST endpoints

-   feature plugins

-   PS admin tools

The separation ensures Praxis remains clean, modular, and maintainable as new wizards and features are introduced.