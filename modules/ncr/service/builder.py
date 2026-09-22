"""
NCR Document Builder
====================
"""

import json


# ------------------------------------------------------------------------------
# Safe normalization helpers
# ------------------------------------------------------------------------------

def normalize_hosts(raw):
    """Ensure hosts are ALWAYS a list of full host strings."""
    if not raw:
        return []
    if isinstance(raw, list):
        return [h.strip() for h in raw if h.strip()]
    if isinstance(raw, str):
        return [h.strip() for h in raw.splitlines() if h.strip()]
    return []


def normalize_gateways(raw):
    """Gateways may be a string or list depending on the form."""
    if not raw:
        return []
    if isinstance(raw, list):
        return [g.strip() for g in raw if g.strip()]
    if isinstance(raw, str):
        return [g.strip() for g in raw.splitlines() if g.strip()]
    return []


# ------------------------------------------------------------------------------
#  Internal/External Table Helpers
# ------------------------------------------------------------------------------

HEADERS = ("NAMESPACE", "NAME", "GATEWAYS", "HOSTS")


def host_is_internal(host: str) -> bool:
    return ".int." in host


def split_endpoints(endpoints: list[dict]):
    """Split endpoints into (internal_rows, external_rows) with normalized hosts."""

    internal = []
    external = []

    for ep in endpoints:
        # Normalize inputs
        gws = normalize_gateways(ep.get("gateways"))
        hosts = normalize_hosts(ep.get("hosts"))

        gws_json = json.dumps(gws, ensure_ascii=False)

        # Do the split
        internal_hosts = [h for h in hosts if host_is_internal(h)]
        external_hosts = [h for h in hosts if not host_is_internal(h)]

        # Append formatted rows
        if internal_hosts:
            internal.append((
                ep["namespace"],
                ep["name"],
                gws_json,
                json.dumps(internal_hosts, ensure_ascii=False),
            ))

        if external_hosts:
            external.append((
                ep["namespace"],
                ep["name"],
                gws_json,
                json.dumps(external_hosts, ensure_ascii=False),
            ))

    return internal, external


def build_fixed_table(rows):
    if not rows:
        return []

    widths = [len(h) for h in HEADERS]

    for row in rows:
        for i, col in enumerate(row):
            widths[i] = max(widths[i], len(str(col)))

    def fmt(row):
        return "  ".join(str(col).ljust(widths[i]) for i, col in enumerate(row))

    return [fmt(HEADERS)] + [fmt(r) for r in rows]


# ------------------------------------------------------------------------------
#  Egress Formatting
# ------------------------------------------------------------------------------

def build_egress_text(eg, env_up: str) -> tuple[str, str]:

    def flag(name):
        return bool(getattr(getattr(eg, name, None), "data", False))

    def list_field(name):
        data = getattr(getattr(eg, name, None), "data", []) or []
        return list(data)

    def text_field(name):
        return (getattr(getattr(eg, name, None), "data", "") or "").strip()

    txt_lines = []
    md_lines = []

    def add(t, m):
        txt_lines.append(t)
        md_lines.append(m)

    from modules.ncr.networking import load_profile, load_presentation, render
    rules = load_profile("catalyst-common")["profile"]["builder"]["egress_rules"]
    templates = load_presentation("ncr-builder")["definition"]["sections"]
    for key, rule in rules.items():
        field = rule["field"]
        context = {"env_up": env_up, "rule": rule}
        if rule["kind"] == "flag":
            enabled = flag(field)
        elif rule["kind"] == "list":
            values = list_field(field)
            enabled = bool(values)
            context.update(p=", ".join(sorted(values)), flows_s=", ".join(sorted(values)).upper())
        else:
            notes = text_field(field) or text_field("egress_notes") or text_field("egress_notes_scope")
            enabled = bool(notes)
            context["notes"] = notes
        if enabled:
            add(render(templates[key + "_text"], context), render(templates[key + "_markdown"], context))
    txt = "\n".join(txt_lines) if txt_lines else templates["empty_text"]
    md = "\n".join(md_lines) if md_lines else templates["empty_markdown"]

    return txt, md


# ------------------------------------------------------------------------------
#  NCR Main Builder
# ------------------------------------------------------------------------------

def build_documents(
    env_up: str,
    endpoints: list[dict],
    allow_ips: list[str],
    paysafe: str,
    port8080: str,
    ad_access: str,
    protocol_url: str,
    shared_vault_url: str,
    egress_form,
):

    internal_rows, external_rows = split_endpoints(endpoints)
    internal_table = build_fixed_table(internal_rows)
    external_table = build_fixed_table(external_rows)

    def toggle(txt_yes, txt_no, value):
        return txt_yes if value in ("yes", "true") else txt_no

    from modules.ncr.networking import load_presentation, load_profile, render
    templates = load_presentation("ncr-builder")["definition"]["sections"]
    targets = load_profile('catalyst-common')['profile']['builder']['ingress_targets']
    templates = {key: render(value, {'target': targets[key.rsplit('_', 1)[0]]})
                 for key, value in templates.items() if key.rsplit('_', 1)[0] in targets}
    paysafe_txt = toggle(templates["paysafe_txt_yes"], templates["paysafe_txt_no"], paysafe)
    port8080_txt = toggle(templates["port8080_txt_yes"], templates["port8080_txt_no"], port8080)
    adacc_txt = toggle(templates["adacc_txt_yes"], templates["adacc_txt_no"], ad_access)

    # ----- TEXT -----
    txt_lines = [
        "**Summary**",
        f"We need network-related changes for {env_up} environments.",
        "",
        "**Context**",
        f"These changes are required for connectivity for **{env_up}** environments.",
        "",
        "**Acceptance criteria**",
        f"{env_up} endpoint whitelisting:",
        "",
        *(internal_table or ["(no internal endpoints)"]),
        "",
        "External access:",
        "",
        *(external_table or ["(no external endpoints)"]),
        "",
        paysafe_txt,
        "",
        port8080_txt,
        "",
        adacc_txt,
        "",
        "Allow IPs:",
        *(allow_ips or []),
        "",
        f"Protocol: {protocol_url}",
        "",
        f"Access to shared vault: {shared_vault_url}",
        "",
    ]

    egress_txt, egress_md = build_egress_text(egress_form, env_up)
    txt_lines.append("**Egress changes**")
    txt_lines.append(egress_txt)

    txt_final = "\n".join(txt_lines).rstrip()

    # ----- MARKDOWN -----
    md = []

    md.append("# Summary")
    md.append(f"Network-related changes required for **{env_up}** environments.")
    md.append("")

    md.append("# Acceptance criteria")
    md.append(f"Internal endpoints for **{env_up}**:")
    md.append("")

    if internal_table:
        md += ["```text", *internal_table, "```", ""]
    else:
        md.append("_No internal endpoints_")
        md.append("")

    md.append("External endpoints:")
    md.append("")

    if external_table:
        md += ["```text", *external_table, "```", ""]
    else:
        md.append("_No external endpoints_")
        md.append("")

    md.append("")
    md.append(paysafe_txt)
    md.append("")
    md.append(port8080_txt)
    md.append("")
    md.append(adacc_txt)
    md.append("")

    md.append("## Allow IPs")
    md += [f"- {ip}" for ip in (allow_ips or [])]
    md.append("")

    md.append(f"**Protocol:** `{protocol_url}`")
    md.append("")
    md.append(f"**Shared Vault:** `{shared_vault_url}`")
    md.append("")

    md.append("## Egress changes")
    md.append("")
    md.append(egress_md)

    md_final = "\n".join(md).rstrip()

    return txt_final, md_final
