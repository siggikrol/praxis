import io
import re
import yaml
import logging
import json
from flask import (
    abort, render_template, redirect, url_for, session, request, Response, current_app
)
from engine.wizards.factory.services import get_alb_config
from engine.wizards import eks as eks_logic
from engine.wizards import aurora as aurora_logic
from .utils import _key, get_env_types_from_session
from .templating import resolve_template
from .spec import EXCLUDE_FROM_SPEC
from .session_seed import seed_cross_step_values
from engine.wizards.forms.ux import apply_ux_hints
from engine.wizards.utils import prune
from engine.wizards.constants.project_constants import (
    ENV_DEFAULT_ENV_TYPES,
    PROJECT_DEFAULT_SUFFIX,
)
from engine.wizards.constants.vpc_constants import default_vpc_profile_for_region
from services.spec_fingerprint import spec_fingerprint

# ---------------------------------------------------------------------
# Configure logger for this module
# ---------------------------------------------------------------------
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def _spec_hash(spec_yaml) -> str:
    return spec_fingerprint(spec_yaml)


def _clear_current_validation(env_slug: str) -> None:
    """Invalidate generated artifacts after wizard data changes or fails validation."""
    for name in (
        "spec_yaml",
        "core_validate_ok_spec_hash",
        "core_validate_latest_job_id",
    ):
        session.pop(_key(env_slug, name), None)
    session.modified = True


def _core_validate_gate_allowed(env_slug: str, job: dict | None = None) -> bool:
    spec_yaml = session.get(_key(env_slug, "spec_yaml"))
    spec_hash = _spec_hash(spec_yaml)
    gate_key = _key(env_slug, "core_validate_ok_spec_hash")
    approved_hash = str(session.get(gate_key) or "").strip()

    # Reset stale approvals whenever the current spec changes.
    if approved_hash and approved_hash != spec_hash:
        session.pop(gate_key, None)
        session.modified = True
        approved_hash = ""

    if isinstance(job, dict):
        job_hash = str(job.get("spec_hash") or "").strip()
        if (
            job.get("state") == "done"
            and job.get("ok") is True
            and job.get("validation_mode") == "full"
            and job.get("run_iac_test") is True
            and spec_hash
            and job_hash
            and job_hash == spec_hash
        ):
            if approved_hash != spec_hash:
                session[gate_key] = spec_hash
                session.modified = True
            approved_hash = spec_hash

    return bool(spec_hash and approved_hash and approved_hash == spec_hash)


def _core_validate_outcome(job: dict | None, current_spec_hash: str) -> dict:
    """Build the finish-page status model for the latest validation run."""
    if not isinstance(job, dict):
        return {}

    state = str(job.get("state") or "").strip().lower()
    ok = job.get("ok")
    job_spec_hash = str(job.get("spec_hash") or "").strip()
    is_current_spec = bool(
        current_spec_hash and job_spec_hash and job_spec_hash == current_spec_hash
    )
    logs = str(job.get("log_tail") or "")
    has_warnings = bool(
        re.search(
            (
                r"validation completed with warnings"
                r"|pass(?:ed)? with warnings"
                r"|\[\s*warn(?:ing)?\s*\]"
                r"|\bwarn(?:ing)?\s+[1-9]\d*\b"
                r"|⚠"
            ),
            logs,
            flags=re.IGNORECASE,
        )
    )

    if state in {"queued", "running"}:
        level = "info"
        label = "Validation is running"
    elif state in {"done", "error"} and ok is False:
        level = "danger"
        label = "Validation failed"
    elif state == "done" and ok is True and has_warnings:
        level = "warning"
        label = "Validation passed with warnings"
    elif state == "done" and ok is True:
        level = "success"
        label = "Validation passed"
    else:
        level = "warning"
        label = "Validation status is unavailable"

    if current_spec_hash and not is_current_spec:
        level = "warning"
        label = f"{label} for an earlier version of this spec"

    return {
        "job_id": str(job.get("job_id") or "").strip(),
        "level": level,
        "label": label,
        "is_current_spec": is_current_spec,
        "has_warnings": has_warnings,
        "state": state,
    }


def _apply_pc_version_to_spec_yaml(spec_yaml: str, pc_version: str) -> str:
    if not isinstance(spec_yaml, str) or not spec_yaml.strip() or not pc_version:
        return spec_yaml

    lines = spec_yaml.splitlines()
    first_banner_idx = next(
        (idx for idx, line in enumerate(lines) if line.startswith("# --- ")),
        len(lines),
    )
    preamble = list(lines[:first_banner_idx])
    remainder = list(lines[first_banner_idx:])

    replaced = False
    for idx, line in enumerate(preamble):
        if re.match(r"^\s*praxis_core_version\s*:", line):
            preamble[idx] = f"praxis_core_version: {pc_version}"
            replaced = True
            break

    if not replaced:
        insert_at = 0
        for idx, line in enumerate(preamble):
            if re.match(r"^\s*spec_type\s*:", line):
                insert_at = idx + 1
                break
        preamble.insert(insert_at, f"praxis_core_version: {pc_version}")

    merged = "\n".join(preamble + remainder)
    if spec_yaml.endswith("\n"):
        merged += "\n"
    return merged


def _resolve_dynamic_form_class(form_ref, env_types):
    """Resolve static or callable form references and expand for environment types."""
    logger.info("Resolving dynamic form: %s with env_types=%s", form_ref, env_types)

    # normalize env_types to list
    if isinstance(env_types, str):
        env_types = [env_types]
    if not isinstance(env_types, (list, tuple)):
        env_types = []

    # Treat direct class references as classes first; some also expose for_envs().
    if isinstance(form_ref, type):
        form_cls = form_ref
    # handle callable form definitions
    elif callable(form_ref):
        try:
            maybe_cls = form_ref()
            logger.info("Callable form_ref executed; got: %s", maybe_cls)
        except Exception as e:
            logger.error("Error calling form_ref: %s", e, exc_info=True)
            return form_ref

        if not isinstance(maybe_cls, type):
            logger.info("Callable returned instance; using its class: %s", maybe_cls.__class__)
            return maybe_cls.__class__
        form_cls = maybe_cls
    else:
        form_cls = form_ref

    # A callable may already return a fully expanded dynamic class. Do not
    # expand it a second time or both legacy and specialized fields can appear.
    if getattr(form_cls, "_env_fields_resolved", False):
        logger.info("Dynamic environment fields already resolved on %s", form_cls)
        return form_cls

    # support Form.for_envs([...]) pattern
    if hasattr(form_cls, "for_envs"):
        try:
            logger.info("Calling for_envs(%s) on %s", env_types, form_cls)
            return form_cls.for_envs(env_types)
        except Exception as e:
            logger.error("Error in for_envs(%s): %s", env_types, e, exc_info=True)

    logger.info("Resolved form class: %s", form_cls)
    return form_cls


def _sync_saved_vpc_custom_region(env_slug: str, previous_region: str, next_region: str) -> None:
    previous_region = (previous_region or "").strip()
    next_region = (next_region or "").strip()
    if not next_region or previous_region == next_region:
        return

    vpc_key = _key(env_slug, "vpc")
    vpc_cfg = session.get(vpc_key)
    if not isinstance(vpc_cfg, dict):
        return

    changed = False
    for env_cfg in vpc_cfg.values():
        if not isinstance(env_cfg, dict):
            continue
        profile_key = (env_cfg.get("vpc_az_profile") or "custom").strip().lower()
        if profile_key != "custom":
            continue

        default_profile_key = default_vpc_profile_for_region(next_region)
        env_cfg["vpc_az_profile"] = default_profile_key or "custom"
        env_cfg["custom_aws_region"] = next_region
        env_cfg["transit_gateway_id"] = ""
        env_cfg["vpc_endpoint_service"] = ""
        env_cfg["zones"] = []
        env_cfg["zone_ids"] = []
        changed = True

    if changed:
        session[vpc_key] = vpc_cfg
        session.modified = True


def wizard_step(env_slug: str, wizard_steps: list[tuple[str, object]], step: str):
    """Render and process a wizard step with proper navigation and seeding."""
    from services.wizard_workspaces import ensure_active_workspace

    workspace = ensure_active_workspace(session, env_slug)
    if request.method == "POST" and workspace.get("access") == "viewer":
        abort(403)
    logger.info("Wizard step requested: env_slug=%s step=%s", env_slug, step)

    step_keys = [s for s, _ in wizard_steps]
    if step not in step_keys:
        logger.warning("Step '%s' not in step_keys; redirecting to first step.", step)
        if step == "camunda_opensearch" and request.method == "POST":
            abort(404)
        return redirect(url_for(f"{request.blueprint}.wizard_step", step=step_keys[0]))

    idx = step_keys.index(step)
    key, form_ref = wizard_steps[idx]
    logger.info("Matched wizard key=%s (index=%s)", key, idx)

    env_types = get_env_types_from_session(env_slug)
    logger.info("Loaded project_settings env_types=%s", env_types)

    form_cls = _resolve_dynamic_form_class(form_ref, env_types)

    # Spacelift dropdown options are cached in-process; refresh on demand and periodically.
    if key == "spacelift_settings":
        from engine.wizards.forms.spacelift_settings import ensure_spacelift_options_cache

        if request.method == "GET" and request.args.get("spacelift_refresh"):
            ensure_spacelift_options_cache(force=True)
            args = request.args.to_dict(flat=True)
            args.pop("spacelift_refresh", None)
            target = url_for(f"{request.blueprint}.wizard_step", step=step, **args)
            return redirect(target)

        if request.method == "GET":
            ensure_spacelift_options_cache(force=False)

    # EKS catalogs (cluster versions + instance types) are fetched from AWS APIs and cached on disk.
    # Allow a manual refresh via query param (mirrors spacelift_refresh UX).
    if key == "eks":
        from engine.wizards.constants.eks_constants import EKS_DEFAULTS
        from services.aws_instance_types.eks_instance_types import refresh_eks_instance_type_cache

        if request.method == "GET" and request.args.get("eks_cluster_versions_refresh"):
            from services.aws_instance_types.eks_cluster_versions import refresh_eks_cluster_versions_cache

            common_cfg = session.get(_key(env_slug, "common"), {}) or {}
            region = (common_cfg.get("aws_region") or "").strip() or None
            refresh_eks_cluster_versions_cache(region)
            args = request.args.to_dict(flat=True)
            args.pop("eks_cluster_versions_refresh", None)
            target = url_for(f"{request.blueprint}.wizard_step", step=step, **args)
            return redirect(target)

        if request.method == "GET" and request.args.get("eks_ami_refresh"):
            from services.aws_instance_types.eks_ami import refresh_eks_ami_cache

            common_cfg = session.get(_key(env_slug, "common"), {}) or {}
            eks_cfg = session.get(_key(env_slug, "eks"), {}) or {}
            region = (common_cfg.get("aws_region") or "").strip() or None
            cluster_version = (
                request.args.get("eks_cluster_version")
                or eks_cfg.get("eks_cluster_version")
                or EKS_DEFAULTS["cluster_version"]
            )
            ami_type = (
                request.args.get("ami_type")
                or eks_cfg.get("ami_type")
                or EKS_DEFAULTS["ami_type"]
            )
            refresh_eks_ami_cache(region, cluster_version=cluster_version, ami_type=ami_type)
            args = request.args.to_dict(flat=True)
            args.pop("eks_ami_refresh", None)
            target = url_for(f"{request.blueprint}.wizard_step", step=step, **args)
            return redirect(target)

        if request.method == "GET" and request.args.get("eks_instance_types_refresh"):
            common_cfg = session.get(_key(env_slug, "common"), {}) or {}
            region = (common_cfg.get("aws_region") or "").strip() or None
            refresh_eks_instance_type_cache(region)
            args = request.args.to_dict(flat=True)
            args.pop("eks_instance_types_refresh", None)
            target = url_for(f"{request.blueprint}.wizard_step", step=step, **args)
            return redirect(target)

    if key == "vault_database":
        if request.method == "GET" and request.args.get("vault_database_engine_versions_refresh"):
            from services.aws_instance_types.aurora_engine_versions import (
                refresh_aurora_engine_version_cache_async,
            )

            common_cfg = session.get(_key(env_slug, "common"), {}) or {}
            region = (common_cfg.get("aws_region") or "").strip() or None
            refresh_aurora_engine_version_cache_async(region, engine="postgres")
            args = request.args.to_dict(flat=True)
            args.pop("vault_database_engine_versions_refresh", None)
            target = url_for(f"{request.blueprint}.wizard_step", step=step, **args)
            return redirect(target)

        if request.method == "GET" and request.args.get("vault_database_instance_classes_refresh"):
            from services.aws_instance_types.aurora_instance_classes import (
                refresh_aurora_instance_class_cache_async,
            )

            common_cfg = session.get(_key(env_slug, "common"), {}) or {}
            vault_cfg = session.get(_key(env_slug, "vault_database"), {}) or {}
            region = (common_cfg.get("aws_region") or "").strip() or None
            engine_version = (
                request.args.get("engine_version")
                or vault_cfg.get("vault_database_engine_version")
            )
            if not engine_version:
                from services.aws_instance_types.aurora_engine_versions import (
                    get_aurora_engine_version_options,
                )

                _groups, choices, _meta = get_aurora_engine_version_options(
                    region=region,
                    engine="postgres",
                    refresh_async_if_stale=False,
                )
                engine_version = choices[0][0] if choices else None
            if engine_version:
                refresh_aurora_instance_class_cache_async(
                    region,
                    engine="postgres",
                    engine_version=str(engine_version),
                )
            args = request.args.to_dict(flat=True)
            args.pop("vault_database_instance_classes_refresh", None)
            target = url_for(f"{request.blueprint}.wizard_step", step=step, **args)
            return redirect(target)

    # Aurora catalogs (engine versions + instance classes) are fetched from the RDS API and cached on disk.
    if key == "aurora":
        from services.aws_instance_types.aurora_engine_versions import refresh_aurora_engine_version_cache
        from services.aws_instance_types.aurora_instance_classes import refresh_aurora_instance_class_cache

        if request.method == "GET" and request.args.get("aurora_engine_versions_refresh"):
            common_cfg = session.get(_key(env_slug, "common"), {}) or {}
            region = (common_cfg.get("aws_region") or "").strip() or None
            aurora_cfg = session.get(_key(env_slug, "aurora"), {}) or {}
            engine = (aurora_cfg.get("engine") or "").strip() or None
            refresh_aurora_engine_version_cache(region, engine=engine)
            args = request.args.to_dict(flat=True)
            args.pop("aurora_engine_versions_refresh", None)
            target = url_for(f"{request.blueprint}.wizard_step", step=step, **args)
            return redirect(target)

        if request.method == "GET" and request.args.get("aurora_instance_classes_refresh"):
            common_cfg = session.get(_key(env_slug, "common"), {}) or {}
            region = (common_cfg.get("aws_region") or "").strip() or None
            aurora_cfg = session.get(_key(env_slug, "aurora"), {}) or {}
            engine = (aurora_cfg.get("engine") or "").strip() or None
            refresh_aurora_instance_class_cache(region, engine=engine)
            args = request.args.to_dict(flat=True)
            args.pop("aurora_instance_classes_refresh", None)
            target = url_for(f"{request.blueprint}.wizard_step", step=step, **args)
            return redirect(target)

    # Redis node types are fetched from the ElastiCache API and cached on disk.
    if key == "redis":
        from services.aws_instance_types.redis_node_types import refresh_redis_node_type_cache

        if request.method == "GET" and request.args.get("redis_node_types_refresh"):
            common_cfg = session.get(_key(env_slug, "common"), {}) or {}
            region = (common_cfg.get("aws_region") or "").strip() or None
            refresh_redis_node_type_cache(region)
            args = request.args.to_dict(flat=True)
            args.pop("redis_node_types_refresh", None)
            target = url_for(f"{request.blueprint}.wizard_step", step=step, **args)
            return redirect(target)

    # Kafka catalogs (versions + broker node instance types) are fetched from AWS and cached on disk.
    if key == "kafka":
        from services.aws_instance_types.kafka_catalog import refresh_kafka_catalog_cache

        if request.method == "GET" and request.args.get("kafka_catalog_refresh"):
            common_cfg = session.get(_key(env_slug, "common"), {}) or {}
            region = (common_cfg.get("aws_region") or "").strip() or None
            refresh_kafka_catalog_cache(region)
            args = request.args.to_dict(flat=True)
            args.pop("kafka_catalog_refresh", None)
            target = url_for(f"{request.blueprint}.wizard_step", step=step, **args)
            return redirect(target)

    # PC Source folder structure is fetched from GitHub and cached on disk.
    # Allow a manual refresh via query param (mirrors EKS/Aurora/Redis UX).
    if key == "pc_source":
        from services.pc_source_scanner.structure_cache import refresh_pc_source_sections

        if request.method == "GET" and request.args.get("pc_source_refresh"):
            branch = (request.args.get("pc_source_branch") or "").strip()
            if branch.lower() == "default":
                branch = ""
            refresh_pc_source_sections(branch or None, force_refresh=True)
            args = request.args.to_dict(flat=True)
            args.pop("pc_source_refresh", None)
            args.pop("pc_source_branch", None)
            target = url_for(f"{request.blueprint}.wizard_step", step=step, **args)
            return redirect(target)

    # Restore previously saved data if available
    saved_data = session.get(_key(env_slug, key))
    if key == "vault_database" and isinstance(saved_data, dict):
        # Migrate workspaces saved before the Vault wrapper exposed the root
        # module's parameter-group family input under its contract name.
        saved_data = dict(saved_data)
        legacy_family = saved_data.pop("vault_database_family", None)
        if legacy_family is not None:
            saved_data.setdefault(
                "vault_database_instance_pgroup_family",
                legacy_family,
            )
    # Load plugin safely from the plugin registry
    plugin_registry = current_app.extensions.get("plugins", {})

    # Normalize env_slug to avoid duplicate blueprint naming issues
    normalized_slug = env_slug.split(".")[0]

    plugin = plugin_registry.get(normalized_slug)
    if plugin is None:
        raise RuntimeError(f"No plugin registered for '{normalized_slug}'")

    suffix = plugin.get("suffix", PROJECT_DEFAULT_SUFFIX)

    # Inject expected_suffix into forms that support it
    init_args = {}

    # Safely check for parameters in __init__
    init_func = getattr(form_cls, "__init__", None)
    init_code = getattr(init_func, "__code__", None)
    varnames = getattr(init_code, "co_varnames", ())

    if "expected_suffix" in varnames:
        init_args["expected_suffix"] = suffix
    # Auto-pass spec_type/env_slug to forms that support it
    if "spec_type" in varnames:
        init_args.setdefault("spec_type", env_slug)
    # Keep VPC custom mode anchored to the wizard's selected AWS region.
    if key == "vpc" and "aws_region" in varnames:
        common_cfg = session.get(_key(env_slug, "common"), {}) or {}
        init_args["aws_region"] = (common_cfg.get("aws_region") or "us-east-1").strip() or "us-east-1"

    if request.method == "POST":
        form = form_cls(request.form, **init_args)
    else:
        form = form_cls(data=saved_data, **init_args)

    logger.info("Instantiated form class: %s with fields: %s",
                form.__class__.__name__, list(form._fields.keys()))

    try:
        seed_cross_step_values(env_slug, key, form)
        logger.info("Cross-step seeding done for key=%s", key)
    except Exception as e:
        logger.warning("seed_cross_step_values failed for key=%s: %s", key, e)

    if key == "camunda_opensearch":
        from engine.wizards.forms.camunda_opensearch import seed_camunda_form
        seed_camunda_form(env_slug, form)

    has_prev = idx > 0
    has_next = idx + 1 < len(wizard_steps)
    logger.info("Navigation flags: has_prev=%s has_next=%s", has_prev, has_next)

    try:
        apply_ux_hints(form)
        logger.info("UX hints applied successfully for %s", key)
    except Exception as e:
        logger.warning("apply_ux_hints failed for %s: %s", key, e)

    # Handle ALB dynamic add/remove actions before validation; also seed defaults.
    if key == "alb":
        alb_logic = get_alb_config()
        resp = alb_logic.post_actions(env_slug, wizard_steps, idx, key, form)
        if resp:
            return resp
        alb_logic.prefill_groups(form)

    # Handle EKS additional node group add/remove before validation.
    if key == "eks":
        resp = eks_logic.post_actions(env_slug, wizard_steps, idx, key, form)
        if resp:
            return resp

    # Handle Aurora engine profile defaults before validation.
    if key == "aurora":
        resp = aurora_logic.post_actions(env_slug, wizard_steps, idx, key, form)
        if resp:
            return resp
        aurora_logic.prefill_form_defaults(env_slug, form)

    # Debug capture for pc_source submissions to see raw payload
    if key == "pc_source":
        try:
            raw_form = {k: request.form.getlist(k) for k in request.form.keys()}
            logger.info("pc_source request form keys=%s", raw_form)
        except Exception as e:
            logger.warning("pc_source form logging failed: %s", e)

    if not (key == "camunda_opensearch" and request.form.get("opensearch_catalog_refresh") in ("1", "versions")) and form.validate_on_submit():
        logger.info("Form validated successfully for key=%s", key)
        previous_payload = session.get(_key(env_slug, key))
        previous_component_profile = session.get(_key(env_slug, "component_profile"))

        previous_common_region = ""
        if key == "common":
            previous_common = session.get(_key(env_slug, "common"), {}) or {}
            if isinstance(previous_common, dict):
                previous_common_region = (previous_common.get("aws_region") or "").strip()

        payload = prune(form.data)

        # Ensure environment_type only contains base names
        if key == "project_settings":
            if request.form.get("upgrade_component_profile") == "yes":
                if workspace.get("workspace_origin") == "readiness":
                    abort(400, "Update the readiness snapshot to change component scope.")
                from engine.wizards.components import new_component_profile
                profile = new_component_profile(env_slug)
                if profile:
                    session[_key(env_slug, "component_profile")] = profile
                    _clear_current_validation(env_slug)
            if "environment_type" in payload and isinstance(payload["environment_type"], list):
                env_types = payload["environment_type"]
                normalized = []
                seen = set()
                for e in env_types:
                    if not e:
                        continue
                    base = str(e).strip().lower()
                    if "-" in base:
                        base = base.split("-")[0]
                    if base not in seen:
                        normalized.append(base)
                        seen.add(base)
                logger.info("Normalizing environment_type: %s -> %s", env_types, normalized)
                payload["environment_type"] = normalized

            # Normalize environment_suffixes keys to be base names only
            if "environment_suffixes" in payload:
                try:
                    raw_suffixes = payload["environment_suffixes"]
                    if isinstance(raw_suffixes, str):
                        import json
                        suffix_map = json.loads(raw_suffixes)
                    else:
                        suffix_map = raw_suffixes

                    if isinstance(suffix_map, dict):
                        new_suffix_map = {}
                        for k, v in suffix_map.items():
                            base_k = k.split("-")[0] if "-" in k else k
                            new_suffix_map[base_k] = v
                        payload["environment_suffixes"] = json.dumps(new_suffix_map)
                        logger.info("Normalizing environment_suffixes keys: %s", new_suffix_map)
                except Exception as e:
                    logger.warning("Failed to normalize environment_suffixes: %s", e)

        if key == "aurora":
            payload = aurora_logic.normalize_payload(env_slug, payload)
        if key == "camunda_opensearch":
            from engine.wizards.forms.camunda_opensearch import normalize_payload
            payload = normalize_payload(payload)
        # Protect pc_source selections from being wiped when a submit carries no choices.
        if key == "pc_source":
            prev = session.get(_key(env_slug, key)) or {}
            logger.info("pc_source previous session=%s", {kk: vv for kk, vv in prev.items() if kk != "csrf_token"})
            def _all_empty(d: dict) -> bool:
                if not isinstance(d, dict):
                    return True
                for kk, vv in d.items():
                    if kk in ("profile", "profile_change", "csrf_token"):
                        continue
                    if isinstance(vv, (list, tuple, set)) and any(vv):
                        return False
                    if isinstance(vv, str) and vv.strip():
                        return False
                return True

            if _all_empty(payload) and isinstance(prev, dict) and not _all_empty(prev):
                logger.info("pc_source submit had no selections; keeping previous non-empty session values")
                payload = prev
            else:
                logger.info("pc_source saving non-empty selections=%s", {kk: vv for kk, vv in payload.items() if kk != "csrf_token"})

        if key == "pc_source":
            logger.info("pc_source save payload=%s", {kk: vv for kk, vv in payload.items() if kk != "csrf_token"})

        session[_key(env_slug, key)] = payload
        wizard_data_changed = (
            previous_payload != payload
            or previous_component_profile != session.get(_key(env_slug, "component_profile"))
        )
        if wizard_data_changed:
            _clear_current_validation(env_slug)
        if key == "common":
            _sync_saved_vpc_custom_region(
                env_slug,
                previous_common_region,
                (payload.get("aws_region") or "").strip() if isinstance(payload, dict) else "",
            )
        session.modified = True  # 🔸 ensure persistence
        from services.wizard_workspaces import sync_workspace

        next_workspace_step = wizard_steps[idx + 1][0] if has_next else "finish"
        sync_workspace(
            workspace_id=str(workspace["id"]),
            owner=str(workspace["owner"]),
            env_slug=env_slug,
            session_obj=session,
            current_step=next_workspace_step,
            spec_yaml="" if wizard_data_changed else None,
            latest_job_id="" if wizard_data_changed else None,
        )
        logger.debug("Saved form data for %s", key)
        if has_next:
            target = url_for(f"{request.blueprint}.wizard_step", step=wizard_steps[idx + 1][0])
        else:
            target = url_for(f"{request.blueprint}.wizard_finish")
        logger.info("Redirecting to next target: %s", target)
        return redirect(target)

    template_name = resolve_template(key)
    logger.info("Rendering template=%s for step=%s", template_name, key)

    repo_cfg = session.get(_key(env_slug, "repository_settings"), {}) or {}
    repo_prefix = (repo_cfg.get("new_prefix") or "").strip()
    repo_prefix_token = repo_prefix.split("-", 1)[0].lower() if repo_prefix else ""
    spacelift_filter = repo_prefix_token
    if key == "spacelift_settings":
        meta = getattr(form, "_spacelift_meta", None)
        if isinstance(meta, dict) and (meta.get("fallback") or meta.get("empty")):
            spacelift_filter = ""

    return render_template(
        template_name,
        env_slug=env_slug,   # ← ADD THIS
        form=form,
        step=step,
        step_title=f"{env_slug.upper()}: {key.replace('_', ' ').title()}",
        has_prev=has_prev,
        has_next=has_next,
        prev_step=wizard_steps[idx - 1][0] if has_prev else None,
        next_step=wizard_steps[idx + 1][0] if has_next else None,
        spacelift_filter=spacelift_filter,
        repo_prefix=repo_prefix,
        wizard_workspace=workspace,
        workspace_finish_url=(
            url_for("open_wizard_workspace_finish", workspace_id=workspace["id"])
            if workspace.get("spec_yaml") else ""
        ),
        workspace_validation_url=(
            url_for(
                f"{env_slug}_wizard.wizard_core_validate_job_page",
                job_id=workspace["latest_job_id"],
            ) if workspace.get("latest_job_id") else ""
        ),
    )

def wizard_reset(env_slug: str):
    print("SESSION DUMP", list(session.keys()))

    """Completely erase all wizard-related session data for this environment."""
    cleared = []

    # print current session contents for debug
    logger.info("SESSION BEFORE RESET: %s", list(session.keys()))

    # Remove any key that references the current env or generic wizard keys
    for key in list(session.keys()):
        if env_slug in key or key.startswith("wizard"):
            cleared.append(key)
            session.pop(key, None)

    session.modified = True
    logger.info("Wizard RESET complete for env_slug=%s; removed keys=%s", env_slug, cleared)

    # Redirect to first step of a clean wizard
    # return redirect(url_for(f"{env_slug}.wizard_step", step="project_settings"))
    return redirect(url_for(f"{request.blueprint}.wizard_step", step="project_settings"))


def wizard_core_validate_job_start(env_slug: str):
    """Start async Praxis Core dryrun validation for the current finish spec."""
    from .core_validate_jobs import feature_state, start_validation_job
    from services.wizard_workspaces import ensure_active_workspace

    workspace = ensure_active_workspace(session, env_slug)
    if workspace.get("access") == "viewer":
        abort(403)

    def _wants_json() -> bool:
        if (request.args.get("format") or "").strip().lower() == "json":
            return True
        if (request.headers.get("X-Requested-With") or "").strip().lower() == "fetch":
            return True
        accept = (request.headers.get("Accept") or "").lower()
        if "application/json" in accept and "text/html" not in accept:
            return True
        return False

    def _error_response(msg: str, status_code: int = 400):
        if _wants_json():
            return {"ok": False, "error": msg}, status_code
        return redirect(url_for(f"{request.blueprint}.wizard_finish", core_validate_error=msg))

    try:
        state = feature_state()
        if not state.get("enabled"):
            return _error_response(state.get("reason") or "Core validation unavailable")

        spec_yaml = session.get(_key(env_slug, "spec_yaml"))
        if not spec_yaml:
            return _error_response("Spec YAML not found in session. Visit Finish again to regenerate it.")

        # Every new validation attempt must earn a fresh approval for this spec.
        session.pop(_key(env_slug, "core_validate_ok_spec_hash"), None)
        session.modified = True

        defaults = state.get("defaults") if isinstance(state.get("defaults"), dict) else {}
        is_prod = bool(current_app.config.get("RUNTIME_ENV_IS_PROD"))
        pc_version_key = _key(env_slug, "core_validate_pc_version")
        harbor_project_key = _key(env_slug, "core_validate_harbor_project")
        stored_pc_version = str(session.get(pc_version_key) or "").strip()
        stored_harbor_project = str(session.get(harbor_project_key) or "").strip()
        if is_prod:
            harbor_project = (defaults.get("harbor_registry_project") or "").strip()
            pc_version = (defaults.get("pc_version") or "").strip()
        else:
            harbor_project = (
                (
                    request.form.get("harbor_registry_project")
                    or stored_harbor_project
                    or defaults.get("harbor_registry_project")
                    or ""
                )
                .strip()
            )
            pc_version = (
                (
                    request.form.get("pc_version")
                    or stored_pc_version
                    or defaults.get("pc_version")
                    or ""
                )
                .strip()
            )
        spec_yaml_for_run = _apply_pc_version_to_spec_yaml(spec_yaml, pc_version)
        validation_mode = (
            "offline" if request.form.get("skip_s3_download") else "full"
        )
        run_iac_test = bool(
            validation_mode == "full" and request.form.get("run_iac_test")
        )

        job = start_validation_job(
            env_slug=env_slug,
            spec_yaml=spec_yaml_for_run,
            harbor_registry_project=harbor_project,
            pc_version=pc_version,
            validation_mode=validation_mode,
            run_iac_test=run_iac_test,
        )
    except ValueError:
        return _error_response("Invalid core validation request")
    except RuntimeError:
        return _error_response("Core validation unavailable")
    except Exception:
        logger.error(
            "Unexpected error while starting core validation for %s",
            env_slug,
            exc_info=True,
        )
        return _error_response("Core validation request failed", status_code=500)

    if pc_version:
        session[pc_version_key] = pc_version
    if harbor_project:
        session[harbor_project_key] = harbor_project
    if spec_yaml_for_run and spec_yaml_for_run != spec_yaml:
        session[_key(env_slug, "spec_yaml")] = spec_yaml_for_run
    if harbor_project or pc_version or (spec_yaml_for_run and spec_yaml_for_run != spec_yaml):
        session.modified = True

    job_id = job.get("job_id")
    if job_id:
        session[_key(env_slug, "core_validate_latest_job_id")] = job_id
        session.modified = True
        from services.wizard_workspaces import sync_workspace

        sync_workspace(
            workspace_id=str(workspace["id"]),
            owner=str(workspace["owner"]),
            env_slug=env_slug,
            session_obj=session,
            current_step="finish",
            spec_yaml=str(session.get(_key(env_slug, "spec_yaml")) or ""),
            latest_job_id=str(job_id),
        )
    run_url = url_for(f"{request.blueprint}.wizard_core_validate_job_page", job_id=job_id)
    job["poll_url"] = url_for(
        f"{request.blueprint}.wizard_core_validate_job_status",
        job_id=job_id,
    )
    job["run_url"] = run_url
    if _wants_json():
        return job, 202
    return redirect(run_url)


def wizard_core_validate_job_page(env_slug: str, job_id: str):
    """Render the dedicated Praxis Core validation run page."""
    from .core_validate_jobs import get_job

    if not re.fullmatch(r"[a-f0-9]{32}", (job_id or "").strip()):
        return "invalid job id", 400

    job = get_job(job_id)
    if not job:
        return "job not found", 404

    if str(job.get("env_slug") or "") != env_slug:
        return "job not found", 404

    from services.wizard_workspaces import (
        current_owner,
        get_workspace_by_job,
        load_workspace_state,
    )

    job_workspace = get_workspace_by_job(current_owner(session), job_id)
    if not job_workspace:
        abort(404)
    load_workspace_state(job_workspace, session)

    can_continue = (
        job_workspace.get("access") in {"owner", "editor"}
        and _core_validate_gate_allowed(env_slug, job=job)
    )
    current_spec_hash = _spec_hash(session.get(_key(env_slug, "spec_yaml")))
    job_spec_hash = str(job.get("spec_hash") or "").strip()
    job = dict(job)
    job["can_continue_github"] = can_continue
    job["continue_github_url"] = (
        url_for("github.github_workflow", env_slug=env_slug) if can_continue else ""
    )
    job["can_rerun"] = job_workspace.get("access") in {"owner", "editor"}
    job["rerun_label"] = (
        "Run Validation Again"
        if current_spec_hash and job_spec_hash == current_spec_hash
        else "Validate Current Spec"
    )
    job["rerun_url"] = url_for(
        f"{request.blueprint}.wizard_core_validate_job_start"
    )

    return render_template(
        "core_validate_run.html",
        env_slug=env_slug,
        job=job,
        status_url=url_for(f"{request.blueprint}.wizard_core_validate_job_status", job_id=job_id),
        finish_url=url_for(
            "open_wizard_workspace_finish",
            workspace_id=job_workspace["id"],
        ),
    )


def wizard_core_validate_job_status(env_slug: str, job_id: str):
    """Return async Praxis Core dryrun validation status payload."""
    from .core_validate_jobs import get_job

    if not re.fullmatch(r"[a-f0-9]{32}", (job_id or "").strip()):
        return {"ok": False, "error": "invalid job id"}, 400

    job = get_job(job_id)
    if not job:
        return {"ok": False, "error": "job not found"}, 404

    # Keep environment runs isolated per wizard.
    if str(job.get("env_slug") or "") != env_slug:
        return {"ok": False, "error": "job not found"}, 404

    from services.wizard_workspaces import current_owner, get_workspace_by_job

    job_workspace = get_workspace_by_job(current_owner(session), job_id)
    if not job_workspace:
        return {"ok": False, "error": "job not found"}, 404

    can_continue = (
        job_workspace.get("access") in {"owner", "editor"}
        and _core_validate_gate_allowed(env_slug, job=job)
    )
    job = dict(job)
    job["can_continue_github"] = can_continue
    job["continue_github_url"] = (
        url_for("github.github_workflow", env_slug=env_slug) if can_continue else ""
    )

    return job, 200


def add_auto_banners(yaml_text: str) -> str:
    """
    Inserts section banners ONLY for top-level YAML keys.
    Prevents banners from appearing inside list items (fix for ALB groups).
    """
    lines = yaml_text.splitlines()
    out = []
    customer_name = None
    customer_re = re.compile(r"^\s*customer:\s*(.+)$")

    for line in lines:
        # detect customer for dynamic PRAXISRELEASE banner
        m = customer_re.match(line)
        if m:
            customer_name = m.group(1).strip()

        # before praxisrelease, insert customer banner
        if line.startswith("praxisrelease:"):
            title = customer_name.upper() if customer_name else "PRAXISRELEASE"
            out.append(f"# --- {title} VERSIONS ---")
            out.append(line)
            continue

        # INSERT BANNER ONLY FOR REAL TOP-LEVEL SECTIONS
        if (
            line
            and not line.startswith(" ")
            and not line.startswith("- ")
            and re.match(r"^[A-Za-z0-9_]+:\s*$", line)
        ):
            section = line.rstrip(":")
            banner = f"# --- {section.replace('_', ' ').upper()} ---"
            out.append(banner)

        out.append(line)

    return "\n".join(out)


def wizard_finish(env_slug: str):
    """Final wizard step – render results and provide download/upload options."""
    from services.wizard_workspaces import ensure_active_workspace
    from engine.wizards.validation_display import validation_display

    workspace = ensure_active_workspace(session, env_slug)
    current_app.logger.info(f"Wizard finish triggered for {env_slug}")

    # Log template search paths for debugging
    try:
        import jinja2
        if isinstance(current_app.jinja_loader, jinja2.ChoiceLoader):
            loaders = []
            for loader in current_app.jinja_loader.loaders:
                if hasattr(loader, "searchpath"):
                    loaders.extend(loader.searchpath)
            current_app.logger.info(f"Template loader search paths: {loaders}")
        else:
            current_app.logger.info(f"Template loader: {current_app.jinja_loader}")
    except Exception as e:
        current_app.logger.error(f"Error logging template paths: {e}", exc_info=True)

    # -----------------------------------------------------------------------
    # 1 Collect wizard data from session
    # -----------------------------------------------------------------------
    prefix = f"{env_slug}:"
    skip = {
        f"{env_slug}:spec_yaml",
        f"{env_slug}:spacelift_aws_accounts",
        f"{env_slug}:core_validate_ok_spec_hash",
        f"{env_slug}:core_validate_pc_version",
        f"{env_slug}:core_validate_harbor_project",
        f"{env_slug}:core_validate_latest_job_id",
        f"{env_slug}:workspace_id",
        # Retain Mkodo implementation for possible future re-enablement, but
        # exclude stale saved values while the step is disabled.
        f"{env_slug}:mkodo",
        # Jinja2/Grafana wizard cards are retired. Ignore values retained by
        # workspaces created before 4.0 so they cannot leak into fresh specs.
        f"{env_slug}:jinja2",
        f"{env_slug}:grafana",
    }
    data = {}
    for k, v in session.items():
        if not k.startswith(prefix) or k in skip:
            continue
        data[k.split(":", 1)[1]] = v
    if not data:
        current_app.logger.warning("No data found for the finish step")
        return redirect(url_for(f"{env_slug}.env_home"))
    current_app.logger.info("Finish step: initial wizard keys=%s", sorted(data.keys()))

    # Normalize legacy key from early unified wizard iterations.
    if "subnets" not in data and "subnets_default" in data:
        current_app.logger.info("Normalizing subnets_default -> subnets for %s", env_slug)
        data["subnets"] = data.pop("subnets_default")

    from .core_validate_jobs import feature_state, get_job

    core_validate = feature_state()
    defaults = core_validate.get("defaults") if isinstance(core_validate.get("defaults"), dict) else {}
    default_pc_version = str(defaults.get("pc_version") or "").strip()
    default_harbor_project = str(defaults.get("harbor_registry_project") or "").strip()
    is_prod = bool(current_app.config.get("RUNTIME_ENV_IS_PROD"))
    pc_version_key = _key(env_slug, "core_validate_pc_version")
    harbor_project_key = _key(env_slug, "core_validate_harbor_project")
    if is_prod:
        selected_pc_version = default_pc_version
        if selected_pc_version:
            session[pc_version_key] = selected_pc_version
            session.modified = True
    else:
        selected_pc_version = str(session.get(pc_version_key) or default_pc_version).strip()
        if selected_pc_version and session.get(pc_version_key) != selected_pc_version:
            session[pc_version_key] = selected_pc_version
            session.modified = True
    if is_prod:
        selected_harbor_project = default_harbor_project
    else:
        selected_harbor_project = str(
            session.get(harbor_project_key) or default_harbor_project
        ).strip()
    if selected_harbor_project and session.get(harbor_project_key) != selected_harbor_project:
        session[harbor_project_key] = selected_harbor_project
        session.modified = True

    # Mark which wizard produced the spec for downstream consumers
    spec_prefix = {"spec_type": env_slug}
    if selected_pc_version:
        spec_prefix["praxis_core_version"] = selected_pc_version
    data = {**spec_prefix, **data}

    # -----------------------------------------------------------------------
    # 2 Load deployment manifest but DO NOT merge into YAML structure
    # -----------------------------------------------------------------------
    release_info = session.get("release_selection")
    release_manifest_error = ""
    if release_info:
        from modules.praxisrelease.service import fetch_release_manifest, BUCKET

        customer = release_info.get("customer")
        release_key = release_info.get("release_key")
        if customer == "all":
            customer = None

        try:
            # Load manifest so UI can show info if needed
            manifest = fetch_release_manifest(BUCKET, release_key, customer)
            data["_release_manifest"] = manifest  # store outside final YAML
            current_app.logger.info(
                f"Loaded release manifest {release_key} (customer={customer}) but not merging into YAML"
            )
        except Exception as first_error:
            # Readiness customer codes are canonical uppercase, while S3
            # customer folders are normally lowercase. Retry the normalized
            # folder so the imported release behaves like a manual selection.
            normalized_customer = str(customer or "").strip().lower() or None
            if normalized_customer and normalized_customer != customer:
                try:
                    manifest = fetch_release_manifest(BUCKET, release_key, normalized_customer)
                    data["_release_manifest"] = manifest
                    release_info = {**release_info, "customer": normalized_customer}
                    session["release_selection"] = release_info
                    session.modified = True
                    current_app.logger.info(
                        "Loaded release manifest %s after normalizing customer to %s",
                        release_key,
                        normalized_customer,
                    )
                except Exception as retry_error:
                    release_manifest_error = (
                        f"Selected release {release_key} could not be loaded from the release catalog. "
                        "Open Change Release, confirm the archive is available, and try again."
                    )
                    current_app.logger.warning(
                        "Could not load release manifest: %s; normalized retry failed: %s",
                        first_error,
                        retry_error,
                    )
            else:
                release_manifest_error = (
                    f"Selected release {release_key} could not be loaded from the release catalog. "
                    "Open Change Release, confirm the archive is available, and try again."
                )
                current_app.logger.warning(f"Could not load release manifest: {first_error}")

    # -----------------------------------------------------------------------
    # 3 Drop any sections explicitly marked as disabled, then apply transforms
    #    (pc_source, vpc, subnets, alb)
    # -----------------------------------------------------------------------
    from .transforms import TRANSFORM_MAP

    def _section_disabled(val):
        if not isinstance(val, dict):
            return False
        flag = val.get("disable_section", val.get("disabled"))
        # Accept only explicit truthy markers; plain strings like "false" should not disable
        if isinstance(flag, str):
            return flag.strip().lower() in ("true", "1", "yes", "on")
        if isinstance(flag, (int, bool)):
            return bool(flag)
        return False

    filtered = {}
    disabled_sections = []
    for k, v in data.items():
        if _section_disabled(v):
            current_app.logger.info("Omitting section %s due to disable flag", k)
            disabled_sections.append(k)
            continue
        # Drop the flag if present so it never surfaces in output
        if isinstance(v, dict) and ("disable_section" in v or "disabled" in v):
            v = dict(v)
            v.pop("disable_section", None)
            v.pop("disabled", None)
        filtered[k] = v
    data = filtered
    if disabled_sections:
        data["_disabled_sections"] = sorted(set(disabled_sections))

    # pc_source → split into discrete whitelist sections; keep ordering stable
    if "pc_source" in data:
        reordered = {}
        for k, v in data.items():
            if k == "pc_source":
                try:
                    transformed = TRANSFORM_MAP["pc_source"](v)
                    current_app.logger.info(
                        "Finish step: pc_source raw=%s transformed_keys=%s",
                        {kk: vv for kk, vv in v.items() if kk != "csrf_token"},
                        list(transformed.keys()) if isinstance(transformed, dict) else type(transformed),
                    )
                    if isinstance(transformed, dict):
                        reordered.update(transformed)
                    else:
                        reordered[k] = v
                except Exception as e:
                    current_app.logger.warning(f"pc_source transform failed: {e}")
                    reordered[k] = v
            else:
                reordered[k] = v
        data = reordered

    # VPC
    if "vpc" in data:
        try:
            data["vpc"] = TRANSFORM_MAP["vpc"](data["vpc"])
        except Exception as e:
            current_app.logger.warning(f"VPC transform failed: {e}")

    # SUBNETS
    if "subnets" in data:
        try:
            data["subnets"] = TRANSFORM_MAP["subnets"](data["subnets"])
            current_app.logger.info("Applied SUBNETS transform")
        except Exception as e:
            current_app.logger.warning(f"SUBNETS transform failed: {e}")

    # RANCHER
    if "rancher" in data:
        try:
            from .transforms import extract_grafana_from_rancher

            rancher_data, grafana_data = extract_grafana_from_rancher(data["rancher"])
            data["rancher"] = TRANSFORM_MAP["rancher"](rancher_data)
            data["grafana"] = grafana_data
            current_app.logger.info("Applied RANCHER transform")
        except Exception as e:
            current_app.logger.warning(f"RANCHER transform failed: {e}")

    # PROJECT SETTINGS
    if "project_settings" in data:
        try:
            data["project_settings"] = TRANSFORM_MAP["project_settings"](data["project_settings"])
            current_app.logger.info("Applied PROJECT_SETTINGS transform")
        except Exception as e:
            current_app.logger.warning(f"PROJECT_SETTINGS transform failed: {e}")

    # EKS
    if "eks" in data:
        try:
            data["eks"] = TRANSFORM_MAP["eks"](data["eks"])
            current_app.logger.info("Applied EKS transform")
        except Exception as e:
            current_app.logger.warning(f"EKS transform failed: {e}")

    # ALB
    if "alb" in data:
        try:
            transformed = TRANSFORM_MAP["alb"](data["alb"])
            data.pop("alb", None)
            if isinstance(transformed, dict):
                data.update(transformed)
            current_app.logger.info("Applied ALB transform")
        except Exception as e:
            current_app.logger.warning(f"ALB transform failed: {e}")

    # ---------------------------------------------------------
    # Reorder keys so external_alb_allowed_ips appears after redis
    # ---------------------------------------------------------
    if "external_alb_allowed_ips" in data:
        ordered = {}
        inserted = False
        for k, v in data.items():
            ordered[k] = v
            if k == "redis" and not inserted:
                ordered["external_alb_allowed_ips"] = data["external_alb_allowed_ips"]
                inserted = True

        # fallback: if redis not found, append at end
        if not inserted:
            ordered["external_alb_allowed_ips"] = data["external_alb_allowed_ips"]

        data = ordered

    # -----------------------------------------------------------------------
    # 4 Extract manifest BEFORE YAML generation
    # -----------------------------------------------------------------------
    manifest = data.pop("_release_manifest", None)
    session.pop(_key(env_slug, "_release_manifest"), None)

    from engine.wizards.component_export import export_components
    component_errors = export_components(data, env_slug, manifest)

    # -----------------------------------------------------------------------
    # 5 Validate transformed spec (excluding manifest)
    # -----------------------------------------------------------------------
    from engine.wizards import validators

    validation_errors = []
    try:
        validation_errors = validators.validate_for_context_generator(data)
    except Exception as e:
        current_app.logger.warning(f"Spec validation raised unexpectedly: {e}", exc_info=True)
    if release_manifest_error:
        validation_errors.append(release_manifest_error)
    from engine.wizards.iac_requirements import validate_iac_packages
    # Component export may also add its package to an existing whitelist. Report
    # each missing package once, keeping the component-specific explanation.
    package_errors = validate_iac_packages(data, manifest)
    from engine.wizards.components import COMPONENTS
    component_packages = {c.module for c in COMPONENTS
        if c.key in data}
    validation_errors.extend(component_errors)
    validation_errors.extend(error for error in package_errors
                             if not any(f'"{package}"' in error for package in component_packages))

    # -----------------------------------------------------------------------
    # 6 Convert final wizard data (NO manifest inside) to YAML
    # -----------------------------------------------------------------------
    data.pop("_disabled_sections", None)
    stream = io.StringIO()
    yaml.safe_dump(
        data,
        stream,
        sort_keys=False,
        default_flow_style=False
    )
    yaml_text = stream.getvalue()
    stream.close()

    # Add banners for all normal wizard sections
    yaml_text = add_auto_banners(yaml_text)

    # -----------------------------------------------------------------------
    # 7 Append deployment versions block at bottom
    # -----------------------------------------------------------------------
    if manifest:
        dv = io.StringIO()
        dv.write("# --- DEPLOYMENT VERSIONS ---\n")
        dv.write(f"# release_archive: {json.dumps(str(release_info.get('release_key') or '').strip())}\n")
        dv.write(f"# release_customer: {json.dumps(str(release_info.get('customer') or '').strip())}\n")
        dv.write(f"bundle: {manifest.get('bundle')}\n")
        dv.write(f"version: {manifest.get('version')}\n")

        for section in ("iac", "helm", "catalyst", "bootstrap", "rgs", "add_on"):
            if section in manifest:
                yaml.safe_dump({section: manifest[section]}, dv, sort_keys=False)

        yaml_text = yaml_text + "\n" + dv.getvalue()
        dv.close()

    # ---------------------------------------------------------
    # FINISH STEP — always store fresh YAML (avoid stale cache)
    # ---------------------------------------------------------
    session_key = _key(env_slug, "spec_yaml")
    if validation_errors:
        current_app.logger.warning(
            "Spec validation failed; clearing generated artifacts. errors=%s",
            validation_errors,
        )
        _clear_current_validation(env_slug)
        if workspace.get("access") != "viewer":
            from services.wizard_workspaces import sync_workspace

            sync_workspace(
                workspace_id=str(workspace["id"]),
                owner=str(workspace["owner"]),
                env_slug=env_slug,
                session_obj=session,
                spec_yaml="",
                latest_job_id="",
            )
    else:
        session[session_key] = yaml_text
        if workspace.get("access") != "viewer":
            from services.wizard_workspaces import sync_workspace

            sync_workspace(
                workspace_id=str(workspace["id"]),
                owner=str(workspace["owner"]),
                env_slug=env_slug,
                session_obj=session,
                current_step="finish",
                spec_yaml=yaml_text,
                latest_job_id=str(
                    session.get(_key(env_slug, "core_validate_latest_job_id")) or ""
                ),
            )

    # -----------------------------------------------------------------------
    # 8 Handle ?download=1 for YAML download
    # -----------------------------------------------------------------------
    if request.args.get("download") == "1":
        if component_errors:
            return Response("Resolve specification validation errors before downloading.", status=400)
        current_app.logger.info(f"Serving YAML download for {env_slug}")
        return Response(
            yaml_text,
            mimetype="text/yaml",
            headers={
                "Content-Disposition": f"attachment; filename={env_slug}_spec.yaml"
            },
        )

    # -----------------------------------------------------------------------
    # 9 Render the finish page
    # -----------------------------------------------------------------------
    download_url = url_for(f"{request.blueprint}.wizard_finish") + "?download=1"
    try:
        current_app.logger.info(f"Rendering finish.html for {env_slug}")
        back_url = url_for(f"{env_slug}.env_home")
        core_validate_gate_passed = _core_validate_gate_allowed(env_slug)
        core_validate_start_url = url_for(f"{request.blueprint}.wizard_core_validate_job_start")
        core_validate_error = (request.args.get("core_validate_error") or "").strip()
        latest_job = None
        latest_job_id = str(
            session.get(_key(env_slug, "core_validate_latest_job_id")) or ""
        ).strip()
        if latest_job_id:
            latest_job = get_job(latest_job_id)
            if latest_job and str(latest_job.get("env_slug") or "") != env_slug:
                latest_job = None
        latest_validation = _core_validate_outcome(
            latest_job,
            _spec_hash(yaml_text),
        )
        latest_validation_url = (
            url_for(
                f"{request.blueprint}.wizard_core_validate_job_page",
                job_id=latest_validation["job_id"],
            )
            if latest_validation.get("job_id")
            else ""
        )
        
        return render_template(
            "finish.html",
            env_slug=env_slug,
            spec_yaml=yaml_text,
            download_url=download_url,
            back_url=back_url,
            validation_errors=validation_errors,
            validation_items=[validation_display(error) for error in validation_errors],
            core_validate=core_validate,
            core_validate_harbor_project=selected_harbor_project,
            core_validate_pc_version=selected_pc_version,
            core_validate_pc_version_locked=is_prod,
            core_validate_gate_passed=core_validate_gate_passed,
            core_validate_start_url=core_validate_start_url,
            core_validate_error=core_validate_error,
            latest_validation=latest_validation,
            latest_validation_url=latest_validation_url,
            wizard_workspace=workspace,
        )
    except Exception as e:
        current_app.logger.error(f"Error rendering finish template: {e}", exc_info=True)
        return "Error rendering finish page.", 500
