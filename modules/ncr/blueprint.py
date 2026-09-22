import logging
from datetime import datetime

from flask import Blueprint, Response, render_template, request, redirect, url_for, session, flash, jsonify, g
from flask_wtf.csrf import validate_csrf
from wtforms.validators import ValidationError
from modules.ncr.forms.ingress_form import IngressForm
from modules.ncr.forms.egress_form import EgressForm
from modules.ncr.constants import PLATFORM_LABELS, build_environment_slug, get_endpoint_pack, resolve_environment_fields
from modules.ncr.service.builder import build_documents
from modules.ncr.networking import NetworkingContentError

log = logging.getLogger("app.ncr")

bp = Blueprint(
    "ncr",
    __name__,
    url_prefix="/ncr",
    template_folder="templates/ncr",
)


@bp.errorhandler(NetworkingContentError)
def networking_content_error(error):
    return Response(str(error), status=422, mimetype='text/plain')


def _build_ingress_form(saved: dict | None = None) -> IngressForm:
    """Preserve saved endpoint choices and only seed defaults for fresh/legacy payloads."""
    normalized_saved = _normalize_ingress_state(saved)
    form = IngressForm(data=normalized_saved) if normalized_saved is not None else IngressForm()
    if normalized_saved is None or "endpoints" not in normalized_saved:
        form.seed_defaults(get_endpoint_pack(form.platform.data))
    return form


def _empty_endpoint() -> dict[str, str]:
    return {
        "namespace": "",
        "name": "",
        "gateways": "",
        "hosts": "",
        "host_template": "",
    }


def _endpoint_label(endpoint: dict) -> str:
    ns = (endpoint.get("namespace") or "").strip() or "ns"
    name = (endpoint.get("name") or "").strip() or "endpoint"
    return f"{ns} / {name}"


def _log_ncr(message: str, **extra) -> None:
    log.info(message, extra={"request_id": getattr(g, "request_id", None), **extra})


def _normalize_ingress_state(payload: dict | None) -> dict | None:
    if payload is None:
        return None
    normalized_payload = dict(payload)
    platform, prefix, suffix = resolve_environment_fields(
        normalized_payload.get("platform"),
        normalized_payload.get("environment_prefix"),
        normalized_payload.get("environment_suffix"),
        normalized_payload.get("environment"),
    )
    normalized_payload["platform"] = platform
    normalized_payload["environment_prefix"] = prefix
    normalized_payload["environment_suffix"] = suffix
    normalized_payload["environment"] = build_environment_slug(platform, prefix, suffix)
    return normalized_payload


def _default_endpoints(platform: str | None) -> list[dict[str, str]]:
    return [
        {
            "namespace": namespace,
            "name": name,
            "gateways": gateways,
            "hosts": "",
            "host_template": template,
        }
        for namespace, name, gateways, template in get_endpoint_pack(platform)
    ]


def _ingress_payload(form: IngressForm, endpoints: list[dict] | None = None) -> dict:
    payload = {
        key: value
        for key, value in dict(form.data).items()
        if key != "csrf_token"
    }
    if endpoints is not None:
        payload["endpoints"] = endpoints
    return _normalize_ingress_state(payload) or payload


def _save_ingress_payload(payload: dict) -> None:
    session["ncr:ingress"] = payload
    session.modified = True


def _redirect_ingress_with_payload(
    payload: dict,
    *,
    focus_index: int | None = None,
    notice: str | None = None,
    category: str = "info",
):
    _save_ingress_payload(payload)
    if notice:
        flash(notice, category)
    anchor = f"ncr-endpoint-row-{focus_index}" if focus_index is not None else None
    return redirect(url_for("ncr.ingress", _anchor=anchor))


def _apply_host_templates(payload: dict) -> dict:
    env = (payload.get("environment") or "").strip()
    dom = (payload.get("domain") or "").strip()
    endpoints = list(payload.get("endpoints") or [])
    for endpoint in endpoints:
        template = endpoint.get("host_template") or ""
        endpoint["hosts"] = (
            str(template)
            .replace("{ENV}", env)
            .replace("{DOMAIN}", dom)
        )
    payload["endpoints"] = endpoints
    return payload


def _redirect_with_default_endpoints(form: IngressForm):
    payload = _apply_host_templates(_ingress_payload(form, _default_endpoints(form.platform.data)))
    platform_label = PLATFORM_LABELS.get((form.platform.data or "").strip().lower(), "Catalyst")
    notice = f"Restored {len(payload.get('endpoints') or [])} {platform_label} default endpoints."
    _log_ncr(
        "ncr_restore_default_endpoints",
        ncr_action="restore_defaults",
        ncr_endpoints_before=len(form.data.get("endpoints") or []),
        ncr_endpoints_after=len(payload.get("endpoints") or []),
        ncr_focus_index=0 if payload.get("endpoints") else None,
        ncr_notice=notice,
        redirect_to=url_for("ncr.ingress", _anchor="ncr-endpoint-row-0") if payload.get("endpoints") else url_for("ncr.ingress"),
    )
    return _redirect_ingress_with_payload(
        payload,
        focus_index=0 if payload.get("endpoints") else None,
        notice=notice,
    )


# ----------------------------
# PAGE 1 — INGRESS
# ----------------------------

@bp.route("/", methods=["GET", "POST"])
def ingress():
    if request.method == "GET":
        saved = session.get("ncr:ingress")
        form = _build_ingress_form(saved)
        _log_ncr(
            "ncr_ingress_get",
            ncr_action="load",
            ncr_endpoints_after=len(form.endpoints.entries),
        )
        return render_template("ingress.html", form=form)

    form = IngressForm()
    action = (request.form.get("_action") or "").strip()
    endpoints_before = list(form.data.get("endpoints") or [])

    if action == "add_endpoint":
        endpoints = list(endpoints_before)
        endpoints.insert(0, _empty_endpoint())
        focus_index = 0
        notice = "Added blank endpoint row at position 1."
        _log_ncr(
            "ncr_ingress_add_endpoint",
            ncr_action="add_endpoint",
            ncr_endpoints_before=len(endpoints_before),
            ncr_endpoints_after=len(endpoints),
            ncr_focus_index=focus_index,
            ncr_notice=notice,
            redirect_to=url_for("ncr.ingress", _anchor=f"ncr-endpoint-row-{focus_index}"),
        )
        return _redirect_ingress_with_payload(
            _ingress_payload(form, endpoints),
            focus_index=focus_index,
            notice=notice,
        )

    if action.startswith("remove_endpoint:"):
        endpoints = list(endpoints_before)
        raw_idx = action.split(":", 1)[1]
        try:
            idx = int(raw_idx)
        except (TypeError, ValueError):
            idx = -1
        removed_label = None
        if 0 <= idx < len(endpoints):
            removed_label = _endpoint_label(endpoints.pop(idx))
        focus_index = min(idx, len(endpoints) - 1) if endpoints else None
        notice = (
            f"Removed endpoint {removed_label}."
            if removed_label
            else "No endpoint was removed."
        )
        _log_ncr(
            "ncr_ingress_remove_endpoint",
            ncr_action=f"remove_endpoint:{idx}",
            ncr_endpoints_before=len(endpoints_before),
            ncr_endpoints_after=len(endpoints),
            ncr_focus_index=focus_index,
            ncr_removed=removed_label,
            ncr_notice=notice,
            redirect_to=url_for("ncr.ingress", _anchor=f"ncr-endpoint-row-{focus_index}") if focus_index is not None else url_for("ncr.ingress"),
        )
        return _redirect_ingress_with_payload(
            _ingress_payload(form, endpoints),
            focus_index=focus_index,
            notice=notice,
        )

    if action == "reset_hosts":
        payload = _apply_host_templates(_ingress_payload(form))
        _log_ncr(
            "ncr_ingress_reset_hosts",
            ncr_action="reset_hosts",
            ncr_endpoints_before=len(endpoints_before),
            ncr_endpoints_after=len(payload.get("endpoints") or []),
            redirect_to=url_for("ncr.ingress"),
        )
        return _redirect_ingress_with_payload(
            payload,
            notice="Reset hostnames from templates.",
        )

    if action == "remove_all_endpoints":
        payload = _ingress_payload(form, [])
        notice = f"Removed all {len(endpoints_before)} endpoints."
        _log_ncr(
            "ncr_ingress_remove_all_endpoints",
            ncr_action="remove_all_endpoints",
            ncr_endpoints_before=len(endpoints_before),
            ncr_endpoints_after=0,
            ncr_notice=notice,
            redirect_to=url_for("ncr.ingress"),
        )
        return _redirect_ingress_with_payload(payload, notice=notice)

    if action == "restore_default_endpoints":
        return _redirect_with_default_endpoints(form)

    if not form.validate_on_submit():
        _log_ncr(
            "ncr_ingress_validation_failed",
            ncr_action=action or "submit",
            ncr_endpoints_before=len(endpoints_before),
            ncr_endpoints_after=len(form.endpoints.entries),
            ncr_valid=False,
            ncr_errors=form.errors,
        )
        return render_template(
            "ingress.html",
            form=form,
        )

    _save_ingress_payload(_ingress_payload(form))
    _log_ncr(
        "ncr_ingress_saved",
        ncr_action=action or "submit",
        ncr_endpoints_after=len(form.data.get("endpoints") or []),
        ncr_valid=True,
        redirect_to=url_for("ncr.egress"),
    )
    return redirect(url_for("ncr.egress"))


@bp.route("/add-endpoint", methods=["POST"])
def add_endpoint():
    form = IngressForm()
    endpoints = list(form.data.get("endpoints") or [])
    endpoints.insert(0, _empty_endpoint())
    focus_index = 0
    notice = "Added blank endpoint row at position 1."
    _log_ncr(
        "ncr_add_endpoint_route",
        ncr_action="add_endpoint_route",
        ncr_endpoints_before=len(form.data.get("endpoints") or []),
        ncr_endpoints_after=len(endpoints),
        ncr_focus_index=focus_index,
        ncr_notice=notice,
        redirect_to=url_for("ncr.ingress", _anchor=f"ncr-endpoint-row-{focus_index}"),
    )
    return _redirect_ingress_with_payload(
        _ingress_payload(form, endpoints),
        focus_index=focus_index,
        notice=notice,
    )


@bp.route("/remove-endpoint/<int:idx>", methods=["POST"])
def remove_endpoint(idx):
    form = IngressForm()
    endpoints = list(form.data.get("endpoints") or [])
    removed_label = None
    if 0 <= idx < len(endpoints):
        removed_label = _endpoint_label(endpoints.pop(idx))
    focus_index = min(idx, len(endpoints) - 1) if endpoints else None
    notice = (
        f"Removed endpoint {removed_label}."
        if removed_label
        else "No endpoint was removed."
    )
    _log_ncr(
        "ncr_remove_endpoint_route",
        ncr_action=f"remove_endpoint_route:{idx}",
        ncr_endpoints_before=len(form.data.get("endpoints") or []),
        ncr_endpoints_after=len(endpoints),
        ncr_focus_index=focus_index,
        ncr_removed=removed_label,
        ncr_notice=notice,
        redirect_to=url_for("ncr.ingress", _anchor=f"ncr-endpoint-row-{focus_index}") if focus_index is not None else url_for("ncr.ingress"),
    )
    return _redirect_ingress_with_payload(
        _ingress_payload(form, endpoints),
        focus_index=focus_index,
        notice=notice,
    )


@bp.route("/remove-all-endpoints", methods=["POST"])
def remove_all_endpoints():
    form = IngressForm()
    endpoints_before = list(form.data.get("endpoints") or [])
    notice = f"Removed all {len(endpoints_before)} endpoints."
    _log_ncr(
        "ncr_remove_all_endpoints_route",
        ncr_action="remove_all_endpoints_route",
        ncr_endpoints_before=len(endpoints_before),
        ncr_endpoints_after=0,
        ncr_notice=notice,
        redirect_to=url_for("ncr.ingress"),
    )
    return _redirect_ingress_with_payload(
        _ingress_payload(form, []),
        notice=notice,
    )


@bp.route("/restore-default-endpoints", methods=["POST"])
def restore_default_endpoints():
    form = IngressForm()
    return _redirect_with_default_endpoints(form)


# ----------------------------
# PAGE 2 — EGRESS
# ----------------------------

@bp.route("/egress", methods=["GET", "POST"])
def egress():
    if request.method == "GET":
        saved = session.get("ncr:egress")
        form = EgressForm(data=saved) if saved else EgressForm()
        _log_ncr(
            "ncr_egress_get",
            ncr_action="egress_get",
            ncr_endpoints_after=len((session.get("ncr:ingress") or {}).get("endpoints") or []),
        )
        return render_template("egress.html", form=form)

    form = EgressForm()
    session["ncr:egress"] = {
        key: value for key, value in form.data.items() if key != "csrf_token"
    }
    _log_ncr(
        "ncr_egress_saved",
        ncr_action="egress_post",
        redirect_to=url_for("ncr.finish"),
    )
    return redirect(url_for("ncr.finish"))


# ----------------------------
# PAGE 3 — FINISH
# ----------------------------

@bp.route("/finish")
def finish():
    ingress_data = session.get("ncr:ingress")
    egress_data  = session.get("ncr:egress")

    if not ingress_data or not egress_data:
        _log_ncr(
            "ncr_finish_redirect_missing_state",
            ncr_action="finish_get",
            ncr_endpoints_after=len((ingress_data or {}).get("endpoints") or []),
            redirect_to=url_for("ncr.ingress"),
        )
        return redirect(url_for("ncr.ingress"))

    egress_form = EgressForm(data=egress_data)

    env_up           = ingress_data.get("environment", "").strip()
    domain           = ingress_data.get("domain", "").strip()
    endpoints        = ingress_data.get("endpoints", [])
    allow_ips        = (ingress_data.get("allow_ips") or "").splitlines()
    paysafe          = ingress_data.get("outgoing_paysafe_allowed")
    port8080         = ingress_data.get("port_8080_cross_vpc")
    ad_access        = ingress_data.get("eu_ngl_dev_ad_access")
    protocol_url     = ingress_data.get("ad_protocol_url")
    shared_vault_url = ingress_data.get("shared_vault_url")

    # Derive hosts from template if the textarea was left blank
    for ep in endpoints:
        if not (ep.get("hosts") or "").strip() and ep.get("host_template"):
            ep["hosts"] = (ep["host_template"]
                           .replace("{ENV}", env_up)
                           .replace("{DOMAIN}", domain))

    txt, md = build_documents(
        env_up, endpoints, allow_ips, paysafe, port8080,
        ad_access, protocol_url, shared_vault_url, egress_form,
    )
    _log_ncr(
        "ncr_finish_render",
        ncr_action="finish_render",
        ncr_endpoints_after=len(endpoints),
    )

    return render_template("ncr_finish.html", txt=txt, md=md)


# ----------------------------
# DRAFT — Save
# ----------------------------

@bp.route("/save_draft", methods=["POST"])
def save_draft():
    ingress_data = session.get("ncr:ingress")
    egress_data  = session.get("ncr:egress")

    if not ingress_data:
        flash("Nothing to save — complete the ingress form first.", "warning")
        return redirect(url_for("ncr.ingress"))

    name = (request.form.get("draft_name") or "").strip() or "Untitled"
    env  = ingress_data.get("environment", "")

    drafts = list(session.get("ncr:drafts", []))
    drafts.append({
        "name":     name,
        "env":      env,
        "saved_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M") + " UTC",
        "ingress":  ingress_data,
        "egress":   egress_data or {},
    })
    session["ncr:drafts"] = drafts
    session.modified = True

    flash(f"Draft \"{name}\" saved.", "success")
    return redirect(url_for("ncr.finish"))


# ----------------------------
# DRAFT — Load
# ----------------------------

@bp.route("/load_draft/<int:idx>")
def load_draft(idx):
    drafts = session.get("ncr:drafts", [])
    if idx < 0 or idx >= len(drafts):
        flash("Draft not found.", "warning")
        return redirect(url_for("ncr.ingress"))

    draft = drafts[idx]
    session["ncr:ingress"] = draft.get("ingress", {})
    session["ncr:egress"]  = draft.get("egress", {})
    session.modified = True

    flash(f"Draft \"{draft['name']}\" loaded.", "success")
    return redirect(url_for("ncr.ingress"))


# ----------------------------
# DRAFT — Delete
# ----------------------------

@bp.route("/delete_draft/<int:idx>", methods=["POST"])
def delete_draft(idx):
    drafts = list(session.get("ncr:drafts", []))
    if 0 <= idx < len(drafts):
        removed = drafts.pop(idx)
        session["ncr:drafts"] = drafts
        session.modified = True
        flash(f"Draft \"{removed['name']}\" deleted.", "success")
    return redirect(url_for("ncr.ingress"))


# ----------------------------
# PREVIEW API
# ----------------------------

@bp.route("/preview", methods=["POST"])
def preview():
    """JSON endpoint — returns {txt, md} from submitted form data.

    API clients must send a valid CSRF token in the X-CSRFToken
    (or X-CSRF-Token) header.
    """
    csrf_token = request.headers.get("X-CSRFToken") or request.headers.get("X-CSRF-Token")
    if not csrf_token:
        return jsonify({"ok": False, "error": "Missing CSRF token. Send it in the X-CSRFToken header."}), 400

    try:
        validate_csrf(csrf_token)
    except ValidationError:
        return jsonify({"ok": False, "error": "Invalid CSRF token."}), 400

    data = request.get_json(silent=True) or {}

    ing = data.get("ingress", {})
    eg  = data.get("egress", {})

    egress_form = EgressForm(data=eg)

    env_up           = (ing.get("environment") or "").strip()
    domain           = (ing.get("domain") or "").strip()
    endpoints        = ing.get("endpoints", [])
    allow_ips        = (ing.get("allow_ips") or "").splitlines()
    paysafe          = ing.get("outgoing_paysafe_allowed", "unknown")
    port8080         = ing.get("port_8080_cross_vpc", "true")
    ad_access        = ing.get("eu_ngl_dev_ad_access", "true")
    protocol_url     = ing.get("ad_protocol_url", "")
    shared_vault_url = ing.get("shared_vault_url", "")

    for ep in endpoints:
        if not (ep.get("hosts") or "").strip() and ep.get("host_template"):
            ep["hosts"] = (ep["host_template"]
                           .replace("{ENV}", env_up)
                           .replace("{DOMAIN}", domain))

    try:
        txt, md = build_documents(
            env_up, endpoints, allow_ips, paysafe, port8080,
            ad_access, protocol_url, shared_vault_url, egress_form,
        )
        return jsonify({"ok": True, "txt": txt, "md": md})
    except Exception:
        log.exception("Failed to build NCR preview documents")
        return jsonify({"ok": False, "error": "Unable to generate preview."}), 400


@bp.route('/templates', methods=['GET', 'POST'])
def template_admin():
    from flask import abort, current_app
    from constants import RANCHER_EKS_CLUSTERS, RANCHER_CLUSTER_TO_URL
    from modules.environment_readiness.blueprint import _is_setup_admin
    from modules.environment_readiness.request_templates import build_request_template, template_text
    from modules.ncr import template_admin as admin
    from modules.ncr.networking import fingerprint
    if current_app.config.get('AUTH_ENABLED', False) and not _is_setup_admin():
        abort(403)
    architecture = request.values.get('architecture', 'catalyst-multi-vpc')
    if architecture not in admin.ARCHITECTURES:
        abort(404)
    head = admin.state(architecture)
    bundle = admin.current_bundle(architecture)
    preview_data = {key: request.form.get('preview.' + key, default) for key, default in {
        'customer': 'Example customer', 'environment_name': 'example-staging', 'dns_domain': 'example.com',
        'aws_region': 'us-east-1', 'requested_network_size': '21', 'target_date': '',
        'rancher_target': '', 'environment_type': 'staging', 'production_classification': 'NONPROD',
    }.items()}
    preview_data['setup_type'] = architecture
    error = None
    preview = ''
    try:
        if request.method == 'POST':
            if current_app.config.get('WTF_CSRF_ENABLED', True):
                validate_csrf(request.form.get('csrf_token'))
            action = request.values.get('_action', 'preview')
            if action == 'restore':
                bundle = admin.revision(architecture, int(request.form['revision']))
            else:
                bundle = admin.edit_bundle(bundle, architecture, request.form)
        from modules.ncr.template_validation import validate_bundle
        validate_bundle(bundle, architecture)
        template = build_request_template('ncr', preview_data, draft_url='', content_bundle=bundle)
        preview = template_text(template)
        # Validate details as well as the normal ticket before accepting a release.
        build_request_template('ncr', preview_data, draft_url='', include_details=True, content_bundle=bundle)
        if request.method == 'POST':
            expected = int(request.form['draft_version']) if request.form.get('draft_version') else None
            if action in ('save', 'restore'):
                admin.save(architecture, bundle, session.get('user') or 'local-admin', expected)
                flash('Template draft saved. Readiness Gate still uses the published version.', 'success')
            elif action == 'publish':
                if fingerprint(bundle) != fingerprint(admin.revision(architecture, expected)):
                    raise ValueError('Save and preview your changes before publishing.')
                published = int(request.form['published_version']) if request.form.get('published_version') else None
                admin.publish(architecture, expected, published)
                flash('Template published. Readiness Gate will use this version for new or refreshed reviews.', 'success')
            elif action != 'preview':
                raise ValueError('Unknown template action.')
            head = admin.state(architecture)
    except (ValueError, ValidationError) as exc:
        error = str(exc)
    profile = bundle[f'profiles/{architecture}']
    presentation = bundle[f"presentations/{profile['presentation']}"]
    return render_template('template_admin.html', architectures=admin.ARCHITECTURES, architecture=architecture,
                           profile=profile, presentation=presentation, head=head, preview_data=preview_data,
                           preview=preview, error=error,
                           rancher_targets=RANCHER_EKS_CLUSTERS,
                           rancher_urls=RANCHER_CLUSTER_TO_URL), (422 if error else 200)
