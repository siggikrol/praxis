# forms/repository_settings.py
import re
from flask_wtf import FlaskForm
from wtforms import StringField
from wtforms.validators import DataRequired, Optional, Regexp

class RepositorySettingsForm(FlaskForm):
    # NOTE: real regex + hints injected in __init__ based on expected_suffix
    new_prefix = StringField(
        "Repo Prefix",
        validators=[DataRequired()],  # Regexp set in __init__
        # normalize: trim + lowercase
        filters=[lambda s: (s or "").strip().lower()],
        render_kw={
            "placeholder": "<customer>-ctlst",
            "required": True,
            "autocomplete": "off",
            "spellcheck": "false",
        },
        description=(
            "Only the base prefix, e.g. 'acme-ctlst'. "
            "SSA will append the environment/stage automatically."
        ),
    )

    repo_description = StringField(
        "Repo Description",
        validators=[DataRequired()],
        filters=[lambda s: (s or "").strip()],
        render_kw={"placeholder": "PRAXIS PGP <customer> Setup", "autocomplete": "off"},
        description="Short description for the repository",
    )

    # NEW: suffix-aware validation + hints
    def __init__(self, *args, expected_suffix: str = "ctlst", **kwargs):
        super().__init__(*args, **kwargs)
        suffix = (expected_suffix or "ctlst").strip().lower()
        pattern = rf"^[a-z]+-{re.escape(suffix)}$"

        self.new_prefix.validators = [
            DataRequired(message="Repo Prefix is required."),
            Regexp(
                pattern,
                message=f"Use <customer>-{suffix} only (lowercase letters). "
                        "Do NOT append -uat/-staging/-prod/etc.",
            ),
        ]
        rk = self.new_prefix.render_kw = dict(self.new_prefix.render_kw or {})
        rk.update({
            "placeholder": f"<customer>-{suffix}",
            "pattern": pattern,
            "title": f"Lowercase letters only followed by '-{suffix}' (e.g. acme-{suffix}).",
            "required": True,
        })
        self.new_prefix.description = (
            f"Only the base prefix, e.g. 'acme-{suffix}'. "
            "SSA will append the environment/stage automatically."
        )
