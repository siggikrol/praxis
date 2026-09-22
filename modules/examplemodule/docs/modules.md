**Praxis Module System -- Complete How-To Guide**
=======================================================

This document explains:

1.  **What a Praxis module is**
2.  **Folder structure**
3.  **How modules are auto-loaded**
4.  **When you *must* modify app.py**
5.  **How to add WTForms + templates**
6.  **How wizard engine modules differ**
7.  **How to safely create new modules**

This guide is designed so **any developer can follow it without breaking the app.**

* * * * *

1\.  What is a Praxis Module?
===================================

Praxis modules are **plug-ins** that extend the application UI with:

-   New pages
-   New forms
-   Additional workflows
-   Integration points
-   Custom business logic

A module:

-   lives under `modules/<module_name>/`
-   exposes a Flask Blueprint
-   has a small `plugin.py` file that registers it with the app
-   automatically appears on the home screen

* * * * *

2\. Required folder structure
================================

Every module **must** follow this basic layout:

```bash
modules/
  examplemodule/
    __init__.py
    plugin.py
    blueprint.py
    forms.py              ← optional
    service.py            ← optional
    templates/
      examplemodule/
        home.html
        form.html         ← optional
```

You may add additional files but **do not break this structure**.

* * * * *

3\. How Praxis autoloads modules
=======================================

`app.py` contains the plugin loader:
`load_plugins(app)`

This loader:

-   scans `modules/*/plugin.py`
-   imports the module
-   calls `register(app)`
-   collects metadata (title, icon, description, home_endpoint)
-   adds it to the home page
-   registers its blueprint (`bp`)
-   handles optional "register()" functions

✔ Any module present under `/modules/<name>` that meets these rules **will automatically appear**.

You **do NOT** need to modify `app.py` to load modules---unless you have **wizard engine** modules (explained later).

* * * * *

4\. How a module is displayed on the homepage
================================================

`plugin.py` must return a dict:

`return {
    "title": "Example Module",
    "icon": "fa-solid fa-flask",
    "category": "Examples",
    "description": "Demonstration module.",
    "home_endpoint": "examplemodule.home",
}`

The UI automatically groups modules by `.category`.

* * * * *

5\. The module contract (MUST FOLLOW)
========================================

Every module must:

### 1\. Provide `plugin.py`

With:

-   A class `Plugin`
-   A function `register(app)`
-   Return metadata for homepage cards
-   Register the blueprint

### 2\. Provide a valid blueprint

`blueprint.py` contains:
`bp = Blueprint("examplemodule", __name__, url_prefix="/examplemodule")`

### 3\. Provide templates in the correct folder

`templates/examplemodule/home.html`

The folder name **must match the blueprint template_folder**.

* * * * *

6\. Example Module --- Correct Minimal Implementation
======================================================

Your working example module (from previous answer) is now the **canonical template** for creating new modules.

* * * * *

7\. How WTForms work in modules
==================================

1.  Define form in `forms.py`
2.  Handle GET/POST in `blueprint.py`
3.  Use `{{ form.hidden_tag() }}` for CSRF
4.  Render fields manually or with Jinja widgets

WTForms automatically integrates with:

-   global CSRFProtect
-   secure cookie session
-   form validation
-   error rendering

* * * * *

8\. IMPORTANT: Wizard Engine Modules (single-vpc, multi-vpc)
===============================================================

Wizard engine modules **are NOT loaded like normal modules**.
They require more than a basic module blueprint:
Wizard engine module = 2 layer architecture
-------------------------------------------

### LAYER 1: UI Launcher (module blueprint)

Example:

`modules/wizard_single_vpc/blueprint.py`

Used for:

-   home page card
-   landing page
-   "Start Wizard" button

### LAYER 2: Engine Blueprint (engine/wizards)

Example:

`engine/wizards/single_vpc/single_vpc.py`

This blueprint:

-   defines `/single-vpc/wizard/<step>`
-   registers dynamic forms
-   manages session namespaces
-   handles navigation
-   drives Jinja template resolution

IMPORTANT RULE:
------------------

**Wizard engine blueprints must be registered in `app.py`.**\
The module loader does NOT load them.

### Why?

Because engine blueprints are not in `/modules/*`.

### Example required patch in app.py:

`from engine.wizards.single_vpc.single_vpc import bp as single_vpc_engine_bp
app.register_blueprint(single_vpc_engine_bp)

from engine.wizards.multi_vpc.multi_vpc import bp as multi_vpc_engine_bp
app.register_blueprint(multi_vpc_engine_bp)`

✔ THIS IS CRITICAL.\
If you skip this, wizard routing WILL break.

* * * * *

9\. When module and engine need linking
==========================================

If a module is a wizard module:

-   Module blueprint URL: `/single-vpc/`
-   Engine blueprint URL: `/single-vpc/wizard/...`

Your module's `/start` handler must redirect into the engine:

`@bp.get("/start")
def start():
    return redirect(url_for("single-vpc.wizard_step", step="project_settings"))`

This is mandatory.

* * * * *

10\. HOW TO CREATE A NEW MODULE --- STEP BY STEP
=================================================

### Step 1: Copy examplemodule

`cp -R modules/examplemodule modules/mynewmodule`

### Step 2: Rename everything inside:

-   folder → `mynewmodule`
-   blueprint name → `"mynewmodule"`
-   home endpoint → `"mynewmodule.home"`
-   template folder → `"templates/mynewmodule/"`

### Step 3: Update plugin:

`class Plugin:
    title = "My New Module"
    icon = "fa-solid fa-rocket"
    category = "Tools"
    home_endpoint = "mynewmodule.home"`

### Step 4: Create home.html

### Step 5: Optional --- add forms, services, or multiple pages

### Step 6: Restart app → module appears automatically

✔ No app.py changes needed\
❌ Unless wizard module → then you must register engine blueprint

* * * * *

11\. COMMON MISTAKES (AVOID THESE)
=====================================

### ❌ Wrong template folder name

Blueprint:

`template_folder="templates/mynewmodule"`

Folder:

`modules/mynewmodule/templates/mynewmodule/  ← must match EXACTLY`

### ❌ Not returning plugin metadata

Your module will not appear on the homepage.

### ❌ Forgetting to register wizard engine in app.py

Wizard will silently redirect into multi-vpc.

### ❌ Using wrong endpoint names

Blueprint endpoint names must never contain hyphens.

### ❌ Mixing module and engine blueprints

Engine blueprints belong in `engine/wizards`, not modules.