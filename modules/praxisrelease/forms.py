from flask_wtf import FlaskForm
from typing import Optional
import math
import os
from wtforms import SelectField
from wtforms.validators import DataRequired
from flask import request, session, current_app
from modules.praxisrelease.service import (
    list_customers,
    list_customer_releases,
    list_all_release_objects,
    filter_release_names_for_wizard,
    filter_release_objects_for_wizard,
    sort_release_objects,
    sort_release_names,
    get_release_prefix,
    BUCKET,
)

STEP_KEY = "praxisrelease:selection"


class PraxisReleaseForm(FlaskForm):
    """Form for selecting customer and release version."""

    # Explicitly tie to your template
    template = "wizard.html"

    # --- Fields ---
    customer = SelectField(
        "Customer",
        choices=[],
        validators=[DataRequired()],
        coerce=str,
        validate_choice=False,
        render_kw={"id": "customer"},
    )

    release_key = SelectField(
        "Release Version",
        choices=[],
        validators=[DataRequired()],
        coerce=str,
        validate_choice=False,
        render_kw={"id": "release_key"},
    )

    def set_release_choices(self, customer: Optional[str]) -> None:
        customer = (customer or "").strip()
        if customer == "all":
            base_prefix = get_release_prefix()
            page_size = int(os.getenv("PS_RELEASES_ALL_PAGE_SIZE", "50"))
            page_size = max(page_size, 1)
            page = max(self.release_page, 1)

            objs = list_all_release_objects(BUCKET)
            objs = filter_release_objects_for_wizard(objs, self.wizard_slug)
            objs = sort_release_objects(objs)
            total = len(objs)
            pages = max(math.ceil(total / page_size), 1)
            page = min(page, pages)
            start = (page - 1) * page_size
            end = start + page_size
            keys = [o["key"] for o in objs[start:end]]

            self.release_key.choices = [
                (k, k[len(base_prefix):] if base_prefix and k.startswith(base_prefix) else k)
                for k in keys
            ] or [("", "No releases found")]
            self.release_total = total
            self.release_pages = pages
            self.release_page = page
            self.release_page_size = page_size
            return

        releases = []
        if customer:
            releases = list_customer_releases(BUCKET, customer)
            releases = filter_release_names_for_wizard(releases, self.wizard_slug)
            releases = sort_release_names(releases)
        self.release_key.choices = [(r, r) for r in releases] or [("", "No releases found")]
        self.release_total = len(releases)
        self.release_pages = 1
        self.release_page = 1
        self.release_page_size = len(releases)

    # --- Constructor ---
    def __init__(self, *args, **kwargs):
        """
        Initializes customer and release dropdowns.
        Keeps current selections across reloads and sessions.
        """
        self.wizard_slug = (
            kwargs.pop("wizard_slug", None)
            or request.values.get("wizard_slug")
            or request.values.get("env_slug")
            or ""
        ).strip().lower()
        super().__init__(*args, **kwargs)
        form_data = request.form or {}
        sess_data = session.get("release_selection", {})
        try:
            self.release_page = max(int(form_data.get("release_page") or 1), 1)
        except (TypeError, ValueError):
            self.release_page = 1
        self.release_total = 0
        self.release_pages = 1
        self.release_page_size = 0

        # -------------------------------
        # 1. Load customers from S3
        # -------------------------------
        try:
            customers = list_customers()
        except Exception as e:
            customers = []
            current_app.logger.warning(f"Could not load customers: {e}")

        self.customer.choices = [("all", "All")] + [(c, c) for c in customers]

        # -------------------------------
        # 2. Determine selected customer
        # -------------------------------
        selected_customer = (
            form_data.get("customer")
            or sess_data.get("customer")
            or ("all" if self.customer.choices else None)
        )
        # Readiness uses the canonical uppercase customer code while release
        # storage commonly uses a lowercase folder. Preserve the catalog's
        # exact value when the two differ only by case.
        if selected_customer and str(selected_customer).lower() != "all":
            selected_customer = next(
                (
                    value
                    for value, _label in self.customer.choices
                    if str(value).lower() == str(selected_customer).lower()
                ),
                selected_customer,
            )
            if selected_customer not in {value for value, _label in self.customer.choices}:
                self.customer.choices.append((selected_customer, selected_customer))
        if selected_customer:
            self.customer.data = selected_customer

        # -------------------------------
        # 3. Populate releases for that customer
        # -------------------------------
        # Only reload releases if not already populated externally (e.g., via /reload)
        if not self.release_key.choices:
            try:
                self.set_release_choices(selected_customer)
                current_app.logger.debug(
                    f"Loaded releases for {selected_customer}"
                )
            except Exception as e:
                current_app.logger.warning(
                    f"Could not list releases for {selected_customer}: {e}"
                )
                self.release_key.choices = [("", "No releases found")]

        # -------------------------------
        # 4. Restore release selection
        # -------------------------------
        selected_release = form_data.get("release_key") or sess_data.get("release_key")

        # A readiness-imported release remains selectable even when the live
        # catalog is temporarily unavailable. Do not silently replace it with
        # the empty "No releases found" sentinel.
        release_values = [val for val, _ in self.release_key.choices]
        if selected_release in release_values:
            self.release_key.data = selected_release
        elif selected_release:
            self.release_key.choices = [
                (selected_release, f"{selected_release} (selected in readiness)"),
                *[(value, label) for value, label in self.release_key.choices if value],
            ]
            self.release_key.data = selected_release
        elif release_values:
            self.release_key.data = release_values[0]
