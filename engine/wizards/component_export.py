"""Component export boundary: exclude stale state and enforce release prerequisites."""
from engine.wizards.components import COMPONENTS, resolve_components
from engine.wizards.camunda_opensearch import build_spec
from engine.wizards.iac_requirements import has_version, missing_package_message


def export_components(data, env_slug, manifest):
    errors = []
    try:
        included = resolve_components(env_slug, data.get("component_profile"))
    except ValueError as exc:
        included = ()
        errors.append(str(exc))
    active = {c.key for c in included}
    for component in COMPONENTS:
        if component.key not in active:
            data.pop(component.key, None)
    # Profile is persisted in workspace state, not an unrecognized Core section.
    data.pop("component_profile", None)
    envs = (data.get("project_settings") or {}).get("environment_type") or []
    if not isinstance(envs, list) or any(not isinstance(env, str) for env in envs):
        return errors + ["Component environments must be a list of environment names."]
    if included and not envs:
        errors.append("Select at least one environment before configuring Catalyst components.")
    for component in included:
        if component.key == "camunda_opensearch":
            output, issues = build_spec(data.get(component.key), envs, multi=env_slug == "multi-vpc", common=data.get("common"))
            data[component.key] = output
            errors.extend(issues)
            if env_slug == "multi-vpc":
                deployments = (data.get("project_settings") or {}).get("deployments") or []
                for env, targets in output.items():
                    for target in targets:
                        if target not in deployments:
                            errors.append(f"Camunda OpenSearch ({env}): EKS target {target} is not selected in Project Settings.")
        iac = manifest.get("iac") if isinstance(manifest, dict) else None
        version = iac.get(component.module) if isinstance(iac, dict) else None
        if not has_version(version):
            errors.append(missing_package_message(component.module, component.label, manifest))
        # A default component cannot be silently dropped by manual PC Source edits.
        # Absent whitelist means unrestricted; creating a one-module whitelist
        # would accidentally remove all the other release modules.
        whitelist = (data.get("iac_whitelists") or {}).get("iac")
        if isinstance(whitelist, list) and component.module not in whitelist:
            whitelist.append(component.module)
    return errors
