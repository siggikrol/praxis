import re

# -------------------------------
# Regex patterns
# -------------------------------
ENV_SEGMENT_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
PLATFORM_CHOICES = [
    ("catalyst", "Catalyst"),
    ("rgs", "RGS"),
    ("loyalty", "Loyalty"),
]
PLATFORM_LABELS = dict(PLATFORM_CHOICES)
PLATFORM_ENV_TOKENS = {
    "catalyst": "ctlst",
    "rgs": "rgs",
    "loyalty": "playon",
}
ENV_NAME_PATTERN = (
    r"^[a-z0-9]+(?:-[a-z0-9]+)*-(?:ctlst|rgs|playon)-[a-z0-9]+(?:-[a-z0-9]+)*$"
)
DOMAIN_PATTERN = r"^(?!-)(?:[A-Za-z0-9-]{1,63}\.)+[A-Za-z]{2,}$"
ENV_SEGMENT_REGEX = re.compile(ENV_SEGMENT_PATTERN)
ENV_NAME_REGEX = re.compile(ENV_NAME_PATTERN)
ENV_NAME_PARSE_REGEX = re.compile(
    r"^(?P<prefix>[a-z0-9]+(?:-[a-z0-9]+)*)-"
    r"(?P<token>ctlst|rgs|playon)-"
    r"(?P<suffix>[a-z0-9]+(?:-[a-z0-9]+)*)$"
)
HOSTNAME_PATTERN = re.compile(r"^(?:\*\.)?(?!-)(?:[A-Za-z0-9-]{1,63}\.)+[A-Za-z]{2,}$")

def build_environment_slug(
    platform: str | None,
    prefix: str | None,
    suffix: str | None,
) -> str:
    normalized_platform = str(platform or "").strip().lower()
    normalized_prefix = str(prefix or "").strip().lower()
    normalized_suffix = str(suffix or "").strip().lower()
    token = PLATFORM_ENV_TOKENS.get(normalized_platform)
    if not token or not normalized_prefix or not normalized_suffix:
        return ""
    return f"{normalized_prefix}-{token}-{normalized_suffix}"


def parse_environment_slug(environment: str | None) -> tuple[str | None, str, str]:
    normalized_environment = str(environment or "").strip().lower()
    match = ENV_NAME_PARSE_REGEX.match(normalized_environment)
    if not match:
        return None, "", ""

    token = match.group("token")
    prefix = match.group("prefix")
    suffix = match.group("suffix")
    for platform, platform_token in PLATFORM_ENV_TOKENS.items():
        if platform_token == token:
            return platform, prefix, suffix
    return None, prefix, suffix


def resolve_environment_fields(
    platform: str | None,
    prefix: str | None,
    suffix: str | None,
    environment: str | None,
) -> tuple[str, str, str]:
    normalized_platform = str(platform or "").strip().lower()
    normalized_prefix = str(prefix or "").strip().lower()
    normalized_suffix = str(suffix or "").strip().lower()

    parsed_platform, parsed_prefix, parsed_suffix = parse_environment_slug(environment)

    if normalized_platform not in PLATFORM_ENV_TOKENS:
        normalized_platform = parsed_platform or "catalyst"
    if not normalized_prefix:
        normalized_prefix = parsed_prefix
    if not normalized_suffix:
        normalized_suffix = parsed_suffix

    return normalized_platform, normalized_prefix, normalized_suffix


def get_endpoint_pack(platform: str | None) -> list[tuple[str, str, str, str]]:
    from .networking import load_profile
    name = str(platform or "").lower()
    name = name if name in {"rgs", "loyalty"} else "catalyst-common"
    return [(e["namespace"], e["name"], e["gateways"], e["host"])
            for e in load_profile(name)["profile"]["endpoints"].values()]


def __getattr__(name):
    # Legacy imports remain supported; active consumers use fresh profile reads.
    if name not in {'DEFAULT_AD_URL', 'DEFAULT_VAULT_URL', 'DEFAULT_ALLOW_IPS',
                    'PARTNER_CHOICES', 'EAST_WEST_CHOICES', 'ENDPOINT_PACKS',
                    'DEFAULT_ENDPOINTS', 'CATALYST_ENDPOINTS', 'RGS_ENDPOINTS', 'LOYALTY_ENDPOINTS'}:
        raise AttributeError(name)
    from .networking import load_profile
    profile = load_profile("catalyst-common")["profile"]
    defaults = profile["defaults"]
    aliases = {"DEFAULT_AD_URL": "ad_url", "DEFAULT_VAULT_URL": "vault_url", "DEFAULT_ALLOW_IPS": "allow_ips"}
    if name in aliases:
        return defaults[aliases[name]]
    if name in {"PARTNER_CHOICES", "EAST_WEST_CHOICES"}:
        return [tuple(row) for row in profile["builder"][name.lower()]]
    if name == "ENDPOINT_PACKS":
        return {p: get_endpoint_pack(p) for p in ("catalyst", "rgs", "loyalty")}
    if name in {"DEFAULT_ENDPOINTS", "CATALYST_ENDPOINTS", "RGS_ENDPOINTS", "LOYALTY_ENDPOINTS"}:
        return get_endpoint_pack(name.split("_")[0].lower())
    raise AttributeError(name)
