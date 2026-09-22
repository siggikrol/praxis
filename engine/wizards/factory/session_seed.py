import re

from flask import current_app, session, request
from .utils import _key, get_env_types_from_session

from engine.wizards.constants.replacements_constants import RANCHER_CONTEXT_TO_CLUSTER
from services.spacelift_api import list_aws_integrations

from .utils import _key


SINGLE_VPC_DEFAULT_NODE_GROUPS = (
    {
        "suffix": "rmq",
        "instance_types": ["t3a.2xlarge"],
        "min_size": 3,
        "max_size": 6,
        "desired_size": 3,
        "capacity_type": "",
        "disk_size": None,
        "iam_role_use_name_prefix": False,
        "ami_id": "",
        "labels": "dedicated: rabbitmq",
        "taints": "dedicated: rabbitmq: NO_SCHEDULE",
    },
    {
        "suffix": "vault",
        "instance_types": ["t3a.2xlarge"],
        "min_size": 3,
        "max_size": 6,
        "desired_size": 3,
        "capacity_type": "",
        "disk_size": None,
        "iam_role_use_name_prefix": False,
        "ami_id": "",
        "labels": "dedicated: vault",
        "taints": "dedicated: vault: NO_SCHEDULE",
    },
)

EKS_TRUE_DEFAULT_FIELDS = (
    "enable_load_balancer_controller_irsa",
    "enable_cluster_autoscaler_irsa",
    "enable_external_secrets_irsa",
    "enable_external_dns_irsa",
    "enable_rancher_access",
)

RANCHER_TRUE_DEFAULT_FIELDS = (
    "enable_meta",
    "fetch_waf",
    "fetch_cert",
    "fetch_vault_db",
    "enable_kubeconfig_secret",
    "include_rabbit_labels",
    "include_vault_labels",
    "include_rds_labels",
    "include_rds_endpoint",
    "include_vault_rds_labels",
    "include_redis_labels",
    "include_waf_labels",
    "istio",
    "vault",
)


def _seed_node_group_choices(subform, group_entry):
    instance_types = getattr(getattr(group_entry, "form", None), "instance_types", None)
    default_types = getattr(subform, "default_node_group_instance_types", None)
    if not instance_types or not default_types:
        return
    if hasattr(default_types, "option_groups"):
        instance_types.option_groups = default_types.option_groups
    instance_types.choices = list(getattr(default_types, "choices", []) or [])


def _single_vpc_node_group_defaults(prefix: str, env_name: str) -> list[dict]:
    env_token = env_name.replace("_", "-")
    name_base = f"{prefix}-{env_token}" if prefix else env_token
    return [
        {
            "name": f"{name_base}-{item['suffix']}",
            "instance_types": item["instance_types"],
            "min_size": item["min_size"],
            "max_size": item["max_size"],
            "desired_size": item["desired_size"],
            "capacity_type": item["capacity_type"],
            "disk_size": item["disk_size"],
            "iam_role_use_name_prefix": item["iam_role_use_name_prefix"],
            "ami_id": item["ami_id"],
            "labels": item["labels"],
            "taints": item["taints"],
        }
        for item in SINGLE_VPC_DEFAULT_NODE_GROUPS
    ]


def seed_cross_step_values(env_slug: str, key: str, form):
    """Restore full cross-step seeding for dependent forms."""
    repo_cfg = session.get(_key(env_slug, "repository_settings"), {}) or {}
    spacelift_cfg = session.get(_key(env_slug, "spacelift_settings"), {}) or {}
    repl_cfg = session.get(_key(env_slug, "replacements"), {}) or {}
    common_cfg = session.get(_key(env_slug, "common"), {}) or {}

    new_prefix = (repo_cfg.get("new_prefix") or "").strip()

    # replacements
    if request.method == "GET" and key == "replacements":
        # One-time migration for sessions created before Multi-VPC used true
        # platform-component defaults. Later explicit user choices are retained.
        defaults_marker = _key(env_slug, "_platform_replacements_defaults_v1")
        if env_slug == "multi-vpc" and not session.get(defaults_marker):
            for field_name in ("istio", "vault"):
                field = getattr(form, field_name, None)
                if field is not None:
                    field.data = "true"
            if isinstance(repl_cfg, dict) and repl_cfg:
                migrated = dict(repl_cfg)
                migrated.update({"istio": "true", "vault": "true"})
                session[_key(env_slug, "replacements")] = migrated
            session[defaults_marker] = True
            session.modified = True

        if hasattr(form, "ssa_prefix"):
            cur = (form.ssa_prefix.data or "").strip()
            if not cur and new_prefix:
                form.ssa_prefix.data = new_prefix
        if hasattr(form, "ssa_iac_git_repo"):
            cur = (form.ssa_iac_git_repo.data or "").strip()
            if not cur and new_prefix:
                form.ssa_iac_git_repo.data = f"{new_prefix}-iac"
        # Set sensible defaults for per-env IaC branches if empty
        for env_name, field in (form._fields or {}).items():
            subform = getattr(field, "form", None)
            branch_field = getattr(subform, "ssa_iac_git_branch", None) if subform else None
            if branch_field and not (branch_field.data or "").strip():
                branch_field.data = env_name
                rk = branch_field.render_kw = dict(branch_field.render_kw or {})
                rk.setdefault("placeholder", env_name)

    # common
    if key == "common":
        is_catalyst_vpc = env_slug in {"single-vpc", "multi-vpc"}
        if is_catalyst_vpc:
            release_cfg = session.get("release_selection", {}) or {}
            release_customer = str(release_cfg.get("customer") or "").strip()
            if release_customer.lower() == "all":
                release_key = str(release_cfg.get("release_key") or "").strip().lstrip("/")
                try:
                    from modules.praxisrelease.service import get_release_prefix

                    release_prefix = str(get_release_prefix() or "").strip().strip("/")
                except Exception:
                    release_prefix = ""
                if release_prefix and release_key.startswith(f"{release_prefix}/"):
                    release_key = release_key[len(release_prefix) + 1:]
                if "/" in release_key:
                    release_customer = release_key.split("/", 1)[0].strip()
            if release_customer and release_customer.lower() != "all" and hasattr(form, "customer"):
                form.customer.data = release_customer.upper()
                customer_kw = form.customer.render_kw = dict(form.customer.render_kw or {})
                customer_kw["readonly"] = True
            if hasattr(form, "product"):
                form.product.data = "CATALYST"
                product_kw = form.product.render_kw = dict(form.product.render_kw or {})
                product_kw["readonly"] = True

    if request.method == "GET" and key == "common":
        candidate_env = (new_prefix or repl_cfg.get("ssa_prefix") or "").strip().lower()
        if hasattr(form, "environment"):
            cur = (form.environment.data or "").strip()
            if not cur and candidate_env:
                form.environment.data = candidate_env

    # rancher
    if request.method == "GET" and key == "rancher":
        def _to_bool(val):
            if isinstance(val, bool):
                return val
            if val is None:
                return None
            s = str(val).strip().lower()
            if s in ("true", "1", "yes", "y", "on"):
                return True
            if s in ("false", "0", "no", "n", "off", ""):
                return False
            return None

        def _alias_from_integration(s: str) -> str:
            s = (s or "").strip()
            if " (" in s:
                s = s.split(" (", 1)[0]
            else:
                resolved = _resolve_integration_name(s)
                if resolved:
                    s = resolved
            s = s.strip().lower().replace("_", "-")
            s = re.sub(r"[^a-z0-9-]+", "-", s)
            s = re.sub(r"-{2,}", "-", s).strip("-")
            return s

        def _integration_id_from_label(s: str) -> str:
            s = (s or "").strip()
            if " (" in s and s.endswith(")"):
                return s.rsplit(" (", 1)[1].rstrip(")").strip().lower()
            return ""

        def _resolve_integration_name(integration_id: str) -> str:
            integration_id = (integration_id or "").strip()
            if not integration_id:
                return ""
            try:
                items = list_aws_integrations()
            except Exception as exc:
                current_app.logger.warning(
                    "Spacelift: unable to resolve integration name: %s", exc
                )
                return ""
            for item in items:
                if not isinstance(item, dict):
                    continue
                if (item.get("id") or "").strip() == integration_id:
                    return (item.get("name") or "").strip()
            return ""

        def _should_seed_alias(current: str, integration_label: str) -> bool:
            if not (current or "").strip():
                return True
            current = current.strip().lower()
            integ_id = _integration_id_from_label(integration_label)
            if integ_id and current == integ_id:
                return True
            if integ_id and re.fullmatch(r"[0-9a-z]{26}", current or ""):
                return True
            # Readiness handoff briefly populated this field with the 12-digit
            # AWS account ID. Treat that legacy value as replaceable so the
            # alias can be derived from the selected Spacelift integration.
            if re.fullmatch(r"[0-9]{12}", current or ""):
                return True
            return False

        env_data = get_env_types_from_session(env_slug)
        env_keys = [e["full"] for e in env_data]
        top_integ = None
        for et_name in env_keys:
            et_map = spacelift_cfg.get(et_name) or {}
            if et_map.get("aws_integration_id"):
                top_integ = et_map["aws_integration_id"]
                break
        if hasattr(form, "aws_account_alias") and top_integ:
            cur = (form.aws_account_alias.data or "").strip()
            if _should_seed_alias(cur, top_integ):
                form.aws_account_alias.data = _alias_from_integration(top_integ)

        for et_name in env_keys:
            integ = (spacelift_cfg.get(et_name) or {}).get("aws_integration_id")
            sub = getattr(form, et_name, None)
            subform = getattr(sub, "form", None) if sub else None
            f = getattr(subform, "aws_account_alias", None) if subform else None
            if integ and f and _should_seed_alias(f.data, integ):
                f.data = _alias_from_integration(integ)

        # Normalize legacy string booleans for toggles (apply to all envs).
        for env_name, field in (form._fields or {}).items():
            subform = getattr(field, "form", None)
            if not subform:
                continue
            for fname in ("istio", "vault", "multi_mesh", "dev_tools"):
                f = getattr(subform, fname, None)
                if not f or not isinstance(f.data, str):
                    continue
                coerced = _to_bool(f.data)
                if coerced is not None:
                    f.data = coerced

        # Seed shared label fields from common settings (apply to all envs).
        common_defaults = {
            "domain_name": (common_cfg.get("domain_name") or "").strip(),
            "common_env_name": (common_cfg.get("environment") or "").strip(),
            "product": (common_cfg.get("product") or "").strip(),
            "customer": (common_cfg.get("customer") or "").strip(),
            "support_organization": (common_cfg.get("support_organization") or "").strip(),
        }

        rancher_cfg = session.get(_key(env_slug, "rancher"), {}) or {}
        for env_name, field in (form._fields or {}).items():
            subform = getattr(field, "form", None)
            if not subform:
                continue
            saved = rancher_cfg.get(env_name, {}) if isinstance(rancher_cfg, dict) else {}
            if env_slug in {"single-vpc", "multi-vpc"}:
                default_true_fields = list(RANCHER_TRUE_DEFAULT_FIELDS)
                if env_slug == "multi-vpc":
                    default_true_fields.append("multi_mesh")
                for field_name in default_true_fields:
                    if isinstance(saved, dict) and field_name in saved:
                        continue
                    toggle = getattr(subform, field_name, None)
                    if toggle is not None:
                        toggle.data = True
            for fname, fval in common_defaults.items():
                if not fval:
                    continue
                if isinstance(saved, dict) and (saved.get(fname) or "").strip():
                    continue
                f = getattr(subform, fname, None)
                if f and not (f.data or "").strip():
                    f.data = fval

        # Seed Kubernetes label toggles from replacements (apply to all envs).
        label_defaults = {
            "istio": repl_cfg.get("istio"),
            "vault": repl_cfg.get("vault"),
        }
        for env_name, field in (form._fields or {}).items():
            subform = getattr(field, "form", None)
            if not subform:
                continue
            saved = rancher_cfg.get(env_name, {}) if isinstance(rancher_cfg, dict) else {}
            for label_key, label_val in label_defaults.items():
                if label_val in (None, ""):
                    continue
                if isinstance(saved, dict) and label_key in saved:
                    continue
                f = getattr(subform, label_key, None)
                if not f:
                    continue
                coerced = _to_bool(label_val)
                if coerced is not None and f.data in (None, ""):
                    f.data = coerced

        # Keep Rancher management cluster aligned with the Rancher context chosen
        # in Replacements. Region/prefix are then derived by the form mapping.
        for env_name, field in (form._fields or {}).items():
            subform = getattr(field, "form", None)
            if not subform:
                continue
            repl_env = repl_cfg.get(env_name, {}) if isinstance(repl_cfg, dict) else {}
            if not isinstance(repl_env, dict):
                continue
            context = (repl_env.get("ssa_rancher_context") or "").strip().lower()
            cluster = RANCHER_CONTEXT_TO_CLUSTER.get(context)
            cluster_field = getattr(subform, "rancher_cluster_name", None)
            if cluster and cluster_field:
                cluster_field.data = cluster
                if hasattr(subform, "_apply_cluster_mappings"):
                    subform._apply_cluster_mappings()

    # eks
    if request.method == "GET" and key == "eks":
        prefix = (
            (repo_cfg.get("new_prefix") or "").strip().lower()
            or (repl_cfg.get("ssa_prefix") or "").strip().lower()
        )
        saved_eks_cfg = session.get(_key(env_slug, "eks"))
        should_seed_single_vpc_groups = env_slug == "single-vpc" and not saved_eks_cfg
        if env_slug in {"single-vpc", "multi-vpc"} and not saved_eks_cfg:
            for field_name in EKS_TRUE_DEFAULT_FIELDS:
                irsa_field = getattr(form, field_name, None)
                if irsa_field is not None:
                    irsa_field.data = "true"
        for env_name, field in (form._fields or {}).items():
            subform = getattr(field, "form", None)
            if not subform:
                continue
            if prefix:
                name_field = getattr(subform, "default_node_group_name", None)
                if name_field:
                    cur = (name_field.data or "").strip()
                    if not cur or cur == "ng0":
                        name_field.data = f"{prefix}-{env_name.replace('_', '-')}-ng0"
            should_seed_multi_vpc_ilp_groups = (
                env_slug == "multi-vpc" and env_name.endswith("_ilp")
            )
            if (
                (should_seed_single_vpc_groups or should_seed_multi_vpc_ilp_groups)
                and hasattr(subform, "additional_node_groups")
            ):
                groups_field = subform.additional_node_groups
                if len(groups_field) == 0:
                    group_defaults = _single_vpc_node_group_defaults(prefix, env_name)
                    if should_seed_multi_vpc_ilp_groups:
                        selected_types = list(
                            getattr(subform.default_node_group_instance_types, "data", None) or []
                        )
                        if selected_types:
                            for group_data in group_defaults:
                                group_data["instance_types"] = selected_types
                    for group_data in group_defaults:
                        groups_field.append_entry(group_data)
                        _seed_node_group_choices(subform, groups_field[-1])

    # sumologic
    if request.method == "GET" and key == "sumologic":
        prefix = (
            (repo_cfg.get("new_prefix") or "").strip().lower()
            or (repl_cfg.get("ssa_prefix") or "").strip().lower()
            or (common_cfg.get("environment") or "").strip().lower()
        )
        if prefix:
            for env_name, field in form._fields.items():
                sub = getattr(field, "form", None)
                if not sub:
                    continue
                for fname in ("endpoints_tokens", "firehoses", "api_gateway", "ses_emails_identity"):
                    f = getattr(sub, fname, None)
                    if f and "<<PREFIX>>" in (f.data or ""):
                        f.data = f.data.replace("<<PREFIX>>", prefix)
                b = getattr(sub, "sumologic_s3_bucket", None)
                if b and not (b.data or "").strip():
                    b.data = f"{prefix}-sumologic-logs-{env_name}"
                    rk = b.render_kw = dict(b.render_kw or {})
                    rk["placeholder"] = f"{prefix}-sumologic-logs-{env_name}"
