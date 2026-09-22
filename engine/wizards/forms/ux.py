# forms/ux.py
from wtforms.validators import DataRequired, InputRequired

def get_env_key(et):
    """Return the unique key for an environment (base name or full name with suffix)."""
    return et["full"] if isinstance(et, dict) else et


def get_env_label(et):
    """Return a human-readable label for an environment."""
    if isinstance(et, dict):
        return et["full"].upper()
    return et.upper()


def apply_ux_hints(form):
    """
    Add consistent UX hints to all fields in a (possibly nested) form:
      - Add HTML 'required' to fields with DataRequired/InputRequired.
      - Mirror description as Bootstrap tooltip attributes on the input.
    Works recursively for FormField children.
    """
    for field in form:
        # Skip CSRF
        if getattr(field, "name", None) == "csrf_token":
            continue

        # Recurse into nested subforms (FormField)
        # WTForms FormField exposes a `.form` with its children.
        subform = getattr(field, "form", None)
        if subform is not None:
            apply_ux_hints(subform)
            # We don't set tooltips/required on the container itself; only on children.
            continue

        # Ensure render_kw exists
        rk = field.render_kw = dict(field.render_kw or {})

        # Add 'required' if this field is logically required
        if any(isinstance(v, (DataRequired, InputRequired))
               for v in (getattr(field, "validators", []) or [])):
            rk.setdefault("required", True)

