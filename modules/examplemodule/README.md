# Example Module (template for new modules)

This is a minimal, self-contained module you can copy to start a new feature. It registers a Flask blueprint, exposes a simple form, and ships its own templates, so you only need to rename and adapt.

---

## Structure

```
modules/examplemodule/
├── __init__.py
├── blueprint.py      # Flask routes
├── forms.py          # WTForms definitions
├── plugin.py         # register(app) hook + metadata
├── service.py        # pure logic helpers
├── templates/
│   └── examplemodule/
│       ├── home.html
│       └── form.html
└── README.md
```

---

## How it works

- `plugin.py` exposes `register(app)`; the app autoloads it and registers the blueprint.
- `register(app)` also writes plugin metadata into `app.extensions["plugins"]` so the UI can list it.
- `blueprint.py` defines routes under `/examplemodule` with a landing page and a form handler.
- `forms.py` defines `ExampleForm` to demonstrate text, number, and select inputs.
- `service.py` is where you put pure business logic called from the view.
- Templates live under `templates/examplemodule/` and are referenced by the blueprint.

---

## Copy/paste recipe for a new module

1) Duplicate this folder to `modules/<yourmodule>/`.
2) Rename occurrences of `examplemodule` in:
   - `blueprint.py` (blueprint name, url_prefix, template folder)
   - `plugin.py` (`Plugin` metadata and `home_endpoint`)
   - Template folder name under `templates/`.
3) Adjust form fields in `forms.py` and logic in `service.py`.
4) Add your module name to `ENABLED_MODULES` in your docker-compose/env.
5) Restart the app; the new module will auto-register.

---

## Quick verification

After restart, check logs for “registered blueprint” and open:
`http://localhost:5000/<yourmodule>/` (or whatever prefix you set).

If you see the landing page and form submit works, your module template is wired correctly.
