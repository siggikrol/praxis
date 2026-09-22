"""Expected release packages from configured services and explicit IaC selections.

Mappings use Core's SSA/spacelift/blueprints/generic.yaml component roots.
Legacy source folders are not automatically packages: extend verified mappings
when introducing another template-backed service.
"""

SECTION_PACKAGES = {
    'vpc': ('praxis_network', 'VPC networking'),
    'subnets': ('praxis_network', 'VPC networking'),
    'eks': ('praxis_eks_cluster_v2', 'EKS'),
    'aurora': ('praxis_rds_aurora', 'Aurora'),
    'redis': ('praxis_elasticcache_cluster', 'Redis'),
    'vault_database': ('vault', 'Vault database'),
    'formkiq': ('formkiq', 'FormKiQ'),
}
SELECTION_PACKAGES = {
    'network': 'praxis_network',
    'eks_cluster': 'praxis_eks_cluster_v2',
    'certificates': 'praxis_certificates',
    'elasticache': 'praxis_elasticcache_cluster',
    'rds_database': 'praxis_rds_aurora',
    'external_dns': 'praxis_iam_irsa_role',
    'support_services': 'praxis_iam_roles',
    'route53': 'praxis_route53-dns_zones',
    'waf': 'praxis_waf',
    'rancher_import_cluster': 'praxis_rancher_import_cluster',
    'rancher_workspace': 'praxis_rancher_workspace',
    'rancher_fleet_cd': 'praxis_rancher_fleet_cd',
    'vault': 'vault',
    'formkiq': 'formkiq',
}


def has_version(value):
    # Match supported release scalar versions; booleans and containers are invalid.
    return isinstance(value, (str, int, float)) and not isinstance(value, bool) and bool(str(value).strip())


class MissingPackageError(str):
    """Plain-text validation message with a safe template navigation hint."""
    deployment_versions_link = True


def missing_package_message(package, reason, manifest=None):
    kind = 'Artiac IaC wrapper' if package.startswith('praxis_') else 'IaC package'
    manifest = manifest if isinstance(manifest, dict) else {}
    identity = ' '.join(str(manifest[key]).strip() for key in ('bundle', 'version')
                        if manifest.get(key) is not None and str(manifest[key]).strip())
    selected = f'The selected version spec "{identity}"' if identity else 'The selected release version spec'
    return MissingPackageError(f'{selected} is missing a version for {kind} '
            f'"{package}" (required by {reason}). Update the release version spec '
            'to reference a published package version, or select a release that includes it.')


def validate_iac_packages(data, manifest):
    expected = {}
    disabled = set(data.get('_disabled_sections') or [])
    for section, (package, label) in SECTION_PACKAGES.items():
        config = data.get(section)
        if (section not in disabled and isinstance(config, dict) and config
                and not config.get('disabled') and not config.get('disable_section')):
            expected.setdefault(package, label)
    selections = (data.get('iac_whitelists') or {}).get('iac') or []
    if isinstance(selections, list):
        for selection in selections:
            if not isinstance(selection, str):
                continue
            package = selection if selection.startswith('praxis_') else SELECTION_PACKAGES.get(selection)
            if package:
                expected.setdefault(package, f'PC Source selection "{selection}"')
    versions = manifest.get('iac') if isinstance(manifest, dict) else None
    versions = versions if isinstance(versions, dict) else {}
    return [missing_package_message(package, reason, manifest) for package, reason in sorted(expected.items())
            if not has_version(versions.get(package))]
