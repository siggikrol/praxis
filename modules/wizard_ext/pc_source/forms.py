from __future__ import annotations

from typing import List, Tuple, Type, Optional
import os

from flask import current_app
from flask_wtf import FlaskForm
from wtforms import SelectField, SelectMultipleField, HiddenField, BooleanField
from wtforms.validators import Optional
from wtforms.widgets import ListWidget, CheckboxInput

from services.pc_source_scanner.provider import get_repo_branches as _get_repo_branches
from services.pc_source_scanner.profiles import get_profile_defaults
from services.pc_source_scanner.provider_types import PCSections
from services.pc_source_scanner.structure_cache import get_pc_source_sections


PROFILE_CHOICES: List[Tuple[str, str]] = [
    ("custom", "Custom selection"),
    ("unified", "Unified"),
    ("catalyst", "Catalyst"),
    ("rgs", "RGS"),
    ("loyalty", "Loyalty"),
    ("full", "Full (all available)"),
]

_ASSETS_DIR_NAME = "assets"


def _sorted_dirs(items) -> Tuple[str, ...]:
    """Deterministic, alphabetical listing with assets filtered out."""
    return tuple(sorted(d for d in items if d != _ASSETS_DIR_NAME))


class PCSourceForm(FlaskForm):
    """
    Dynamic selector for SSA/bootstrap, SSA/deployment, SSA/iac, SSA/helm.

    This version:
      * Uses cached directory lookups to avoid repeated GitHub API calls.
      * Filters out folders named 'assets'.
      * Profile dropdown can drive defaults (including Select all / Clear all).
    """

    profile = SelectField(
        "Profile",
        choices=PROFILE_CHOICES,
        validators=[Optional()],
        description="Optional preset combining sections for a product group.",
    )

    bootstrap = SelectMultipleField(
        "Bootstrap folders",
        choices=[],
        option_widget=CheckboxInput(),
        widget=ListWidget(prefix_label=False),
        validators=[Optional()],
        description="Select bootstrap folders from SSA/bootstrap.",
    )

    deployment = SelectMultipleField(
        "Deployment folders",
        choices=[],
        option_widget=CheckboxInput(),
        widget=ListWidget(prefix_label=False),
        validators=[Optional()],
        description="Select deployment folders from SSA/deployment.",
    )

    iac = SelectMultipleField(
        "IAC folders",
        choices=[],
        option_widget=CheckboxInput(),
        widget=ListWidget(prefix_label=False),
        validators=[Optional()],
        description="Select infrastructure code folders from SSA/iac.",
    )

    helm = SelectMultipleField(
        "Helm charts",
        choices=[],
        option_widget=CheckboxInput(),
        widget=ListWidget(prefix_label=False),
        validators=[Optional()],
        description="Select Helm charts from SSA/helm.",
    )

    helm_iac_cds = SelectMultipleField(
        "Rancher Helm IAC CDS",
        choices=[],
        option_widget=CheckboxInput(),
        widget=ListWidget(prefix_label=False),
        validators=[Optional()],
        description="Select Rancher Helm IaC pipelines (supports nested folders).",
    )

    helm_app_cds = SelectMultipleField(
        "Rancher Helm App CDS",
        choices=[],
        option_widget=CheckboxInput(),
        widget=ListWidget(prefix_label=False),
        validators=[Optional()],
        description="Select Rancher Helm application pipelines.",
    )

    helm_bootstrap_cds = SelectMultipleField(
        "Rancher Helm Bootstrap CDS",
        choices=[],
        option_widget=CheckboxInput(),
        widget=ListWidget(prefix_label=False),
        validators=[Optional()],
        description="Select Rancher Helm bootstrap pipelines.",
    )

    # Marker to detect profile-change submits (so we can stay on the page)
    profile_change = HiddenField()
    branch_change = HiddenField()

    branch_override = BooleanField(
        "Use custom branch",
        default=False,
        validators=[Optional()],
        render_kw={"id": "branch_override"},
        description="Override the default branch for whitelist discovery.",
    )
    branch_name = SelectField(
        "Branch",
        choices=[],
        validators=[Optional()],
        render_kw={"id": "branch_name"},
        description="Branch name to scan (e.g., feature/xyz).",
    )

    def __init__(self, *args, **kwargs) -> None:
        # Ensure attrs exist before WTForms calls process().
        self.branch_enabled = self._branch_override_enabled()
        self._current_branch = None
        super().__init__(*args, **kwargs)

    def process(self, formdata=None, obj=None, data=None, **kwargs):
        """
        Populate choices inside a request context, then let WTForms
        handle binding data. Any failure just logs and leaves fields empty.
        """
        explicit_profile = None
        if formdata is not None and hasattr(formdata, "get"):
            explicit_profile = formdata.get("profile")
        if not explicit_profile and isinstance(data, dict):
            explicit_profile = data.get("profile")

        try:
            self.branch_enabled = self._branch_override_enabled()
            self._current_branch = None
            self._populate_branch_choices()
            branch = self._resolve_branch(formdata=formdata, data=data)
            self._current_branch = branch
            self._populate_choices(branch=branch)
        except Exception as exc:  # pragma: no cover - defensive
            from flask import current_app
            current_app.logger.warning("PCSourceForm: unable to populate choices: %s", exc)

        # Call base implementation AFTER choices are ready
        super().process(formdata, obj, data, **kwargs)

        # Apply the wizard's baked-in profile on the first visit. Explicit POST
        # or session data always wins so manual checkbox changes are preserved.
        baked_profile = str(getattr(self, "_profile_name", "") or "").strip()
        if not explicit_profile and baked_profile:
            self._apply_profile_defaults(baked_profile, force=True, branch=self._current_branch)
        elif not (self.profile.data or "").strip():
            self.profile.data = "custom"
        if self.branch_enabled and not (self.branch_name.data or "").strip():
            default_branch = (os.getenv("PS_PC_SOURCE_BRANCH_DEFAULT") or "main").strip()
            choice_values = [c[0] for c in (self.branch_name.choices or [])]
            if default_branch in choice_values:
                self.branch_name.data = default_branch

        # ------------------------------------------------------------------
        # Apply profile defaults only when the user explicitly switched the
        # dropdown (profile_change marker is set by the template). This keeps
        # manual checkbox picks intact when clicking Next.
        # ------------------------------------------------------------------
        is_profile_change = bool((self.profile_change.data or "").strip())
        if is_profile_change and self.profile.data:
            self._apply_profile_defaults(self.profile.data, force=True, branch=self._current_branch)

    def validate(self, extra_validators=None):
        """
        If the user is just switching profiles, keep them on the page
        so the checkboxes can refresh instead of advancing the wizard.
        """
        if (self.profile_change.data or "").strip() or (self.branch_change.data or "").strip():
            return False
        return super().validate(extra_validators=extra_validators)

    def _populate_choices(self, branch: Optional[str] = None) -> None:
        """
        Fill the field choices from the disk-backed cache.

        This avoids repeated GitHub API calls on every request and keeps both
        GET (enter) and POST (exit) fast even with multiple gunicorn workers.
        """
        # Background refresh threads need Flask app context; keep refresh manual via the wizard link.
        sections, meta = get_pc_source_sections(branch=branch, refresh_async_if_stale=False)
        # Templates can introspect this for debugging/badges.
        self._pc_source_meta = meta

        self.bootstrap.choices = [(d, d) for d in (sections.get("bootstrap") or [])]
        self.deployment.choices = [(d, d) for d in (sections.get("deployment") or [])]
        self.iac.choices = [(d, d) for d in (sections.get("iac") or [])]
        self.helm.choices = [(d, d) for d in (sections.get("helm") or [])]
        self.helm_iac_cds.choices = [(d, d) for d in (sections.get("helm_iac_cds") or [])]
        self.helm_app_cds.choices = [(d, d) for d in (sections.get("helm_app_cds") or [])]
        self.helm_bootstrap_cds.choices = [(d, d) for d in (sections.get("helm_bootstrap_cds") or [])]

    def _populate_branch_choices(self) -> None:
        if not self.branch_enabled:
            self.branch_name.choices = []
            return
        try:
            branches = _get_repo_branches()
        except Exception as exc:
            current_app.logger.warning("PCSourceForm: unable to load branches: %s", exc)
            branches = []
        if not branches:
            branches = ["main"]
        self.branch_name.choices = [(b, b) for b in branches]

    def _branch_override_enabled(self) -> bool:
        val = os.getenv("PS_PC_SOURCE_BRANCH_SWITCH")
        if val is None:
            try:
                val = current_app.config.get("PC_SOURCE_BRANCH_SWITCH", "")
            except Exception:
                val = ""
        return str(val).strip().lower() in ("1", "true", "yes", "on")

    def _resolve_branch(self, *, formdata=None, data=None) -> Optional[str]:
        if not self.branch_enabled:
            return None
        override_val = None
        branch_name = None
        if formdata is not None and hasattr(formdata, "get"):
            override_val = formdata.get("branch_override")
            branch_name = formdata.get("branch_name")
        if override_val is None and isinstance(data, dict):
            override_val = data.get("branch_override")
            branch_name = data.get("branch_name")
        override = str(override_val).strip().lower() in ("1", "true", "yes", "on", "y", "t")
        if not override:
            return None
        branch_name = (branch_name or os.getenv("PS_PC_SOURCE_BRANCH_DEFAULT") or "main").strip()
        return branch_name or None

    def _apply_profile_defaults(self, profile_name: str, force: bool = False, branch: Optional[str] = None) -> None:
        """
        When a profile is given (catalyst / rgs / loyalty / full / custom),
        pre-select sensible defaults for each section.

        force=True is used when user actively changes the dropdown:
          - overrides previous checkbox selections
        force=False is used for initial baked-in profile:
          - respects any existing field.data.
        """

        # First, handle special "full" (select all) and "custom" (clear all)
        all_bootstrap = [k for k, _ in self.bootstrap.choices]
        all_deployment = [k for k, _ in self.deployment.choices]
        all_iac = [k for k, _ in self.iac.choices]
        all_helm = [k for k, _ in self.helm.choices]
        all_helm_iac_cds = [k for k, _ in self.helm_iac_cds.choices]
        all_helm_app_cds = [k for k, _ in self.helm_app_cds.choices]
        all_helm_bootstrap_cds = [k for k, _ in self.helm_bootstrap_cds.choices]

        if profile_name == "full":
            self.profile.data = "full"
            self.bootstrap.data = all_bootstrap
            self.deployment.data = all_deployment
            self.iac.data = all_iac
            self.helm.data = all_helm
            self.helm_iac_cds.data = all_helm_iac_cds
            self.helm_app_cds.data = all_helm_app_cds
            self.helm_bootstrap_cds.data = all_helm_bootstrap_cds
            return

        if profile_name == "custom":
            self.profile.data = "custom"
            self.bootstrap.data = []
            self.deployment.data = []
            self.iac.data = []
            self.helm.data = []
            self.helm_iac_cds.data = []
            self.helm_app_cds.data = []
            self.helm_bootstrap_cds.data = []
            return

        # For other profiles, defer to profile defaults provider
        defaults: PCSections = get_profile_defaults(profile_name, branch=branch)

        valid_keys = {val for val, _ in PROFILE_CHOICES}
        if profile_name in valid_keys:
            self.profile.data = profile_name
        else:
            if not self.profile.data:
                self.profile.data = "custom"

        def _apply(field, names: List[str]) -> None:
            # Don't overwrite user-selected values unless we are forcing
            if field.data and not force:
                return
            choice_keys = {k for k, _ in field.choices}
            field.data = [n for n in names if n in choice_keys]

        _apply(self.bootstrap, defaults.get("bootstrap", []))
        _apply(self.deployment, defaults.get("deployment", []))
        _apply(self.iac, defaults.get("iac", []))
        _apply(self.helm, defaults.get("helm", []))
        _apply(self.helm_iac_cds, defaults.get("helm_iac_cds", []))
        _apply(self.helm_app_cds, defaults.get("helm_app_cds", []))
        _apply(self.helm_bootstrap_cds, defaults.get("helm_bootstrap_cds", []))

        # Mirror base sections into their matching CDS lists when not explicitly set.
        def _mirror(source_field, target_field) -> None:
            if target_field.data:
                return
            if not source_field.data:
                target_field.data = []
                return
            target_keys = {k for k, _ in target_field.choices}
            target_field.data = [n for n in source_field.data if n in target_keys]

        _mirror(self.bootstrap, self.helm_bootstrap_cds)
        _mirror(self.deployment, self.helm_app_cds)
        _mirror(self.helm, self.helm_iac_cds)

    @classmethod
    def for_profile(cls, profile_name: str | None) -> Type["PCSourceForm"]:
        """
        Factory to bake the chosen profile into a subclass, so the wizard
        can ask for PCSourceForm.for_profile("rgs") and get a class with a
        baked-in _profile_name.
        """
        if not profile_name:
            return cls

        class DynamicPCSourceForm(cls):  # type: ignore[misc]
            _profile_name = profile_name

        DynamicPCSourceForm.__name__ = (
            f"{cls.__name__}_{profile_name.replace('-', '_')}"
        )
        return DynamicPCSourceForm
