"""Versioned wizard component catalogue shared with readiness.

Absent profile metadata means a legacy workspace, not the newest defaults.
Optional entries can use the same resolver once their forms/generators exist.
"""
from dataclasses import dataclass
from importlib import import_module


CATALYST_WIZARDS = frozenset({"single-vpc", "multi-vpc"})
PROFILE_VERSION = 1


@dataclass(frozen=True)
class Component:
    key: str
    label: str
    module: str
    wizards: frozenset[str]
    default: bool = False
    after: str = "eks"
    form_class: str = ""


COMPONENTS = (
    Component("camunda_opensearch", "Camunda OpenSearch", "praxis_camunda_opensearch",
              CATALYST_WIZARDS, default=True,
              form_class="engine.wizards.forms.camunda_opensearch.CamundaOpenSearchForm"),
)


def new_component_profile(env_slug):
    return {"version": PROFILE_VERSION, "optional": []} if env_slug in CATALYST_WIZARDS else {}


def resolve_components(env_slug, profile):
    if not profile:
        return ()
    if not isinstance(profile, dict) or type(profile.get("version")) is not int or profile["version"] != PROFILE_VERSION:
        raise ValueError("Unsupported component profile version. Update Studio before continuing.")
    selected = profile.get("optional", [])
    if not isinstance(selected, list) or any(not isinstance(key, str) for key in selected):
        raise ValueError("Optional components must be a list of component IDs.")
    available = {c.key: c for c in COMPONENTS if env_slug in c.wizards}
    if any(key not in available or available[key].default for key in selected):
        raise ValueError("Unknown or unsupported optional component selection.")
    return tuple(c for c in available.values() if c.default or c.key in selected)


def active_steps(env_slug, steps, state):
    """Return a request-local list; never change blueprint/global step lists."""
    known = {c.key for c in COMPONENTS}
    result = [(key, form) for key, form in steps if key not in known]
    for component in resolve_components(env_slug, state.get(f"{env_slug}:component_profile")):
        index = next(i for i, (key, _) in enumerate(result) if key == component.after)
        module, name = component.form_class.rsplit(".", 1)
        form = getattr(import_module(module), name)
        result.insert(index + 1, (component.key, lambda form=form: form.for_wizard(env_slug)))
    return result


def readiness_components(data):
    slug = {"catalyst-single-vpc": "single-vpc", "catalyst-multi-vpc": "multi-vpc"}.get(data.get("setup_type"), "")
    return resolve_components(slug, data.get("component_profile"))
