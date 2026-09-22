# --- Rancher contexts and their Spacelift AWS Integration IDs ---
RANCHER_CONTEXT_CHOICES = [
    ("", "Select a Rancher context"),
    ("rancher-eu-nonprod", "rancher-eu-nonprod"),
    ("rancher-eu", "rancher-eu"),
    ("rancher-devops",    "rancher-devops"),
    ("rancher-preprod", "rancher-preprod"),
    ("rancher-nonprod",    "rancher-nonprod"),
    ("rancher-prod",    "rancher-prod"),
]

# Map context -> exact Spacelift AWS Integration ID
RANCHER_CONTEXT_TO_INTEGRATION = {
    "rancher-eu-nonprod": "01J6C65MK08K6V5BYCBBZ3PWVF",
    "rancher-eu": "01J6C65MK08K6V5BYCBBZ3PWVF",
    "rancher-devops":    "01HV40K729CJWBEAGT5VWQFQYV",
    "rancher-preprod": "01HV40K729CJWBEAGT5VWQFQYV",
    "rancher-nonprod":    "01HV40K729CJWBEAGT5VWQFQYV",
    "rancher-prod":    "01HV40K729CJWBEAGT5VWQFQYV",
}

RANCHER_CONTEXT_TO_CLUSTER = {
    "rancher-eu-nonprod": "eu-shared-rancher-nonprod",
    "rancher-eu": "ngl-rancher",
    "rancher-devops": "PRAXIS-DevOps",
    "rancher-nonprod": "PRAXIS-Rancher",
    "rancher-preprod": "PRAXIS-Rancher-Preprod",
    "rancher-prod": "PRAXIS-Rancher-Prod",
}

RANCHER_WORKER_POOL_ID_DEFAULT = "01HTGAQXS0SG0RJ6YDKKZ0BZV6"
RANCHER_WORKER_POOL_ID_EU = "01HSV9C59AV2MCSPD15VKF1EW2"


def rancher_worker_pool_id_for_context(context: str | None) -> str:
    key = (context or "").strip().lower()
    return RANCHER_WORKER_POOL_ID_EU if "eu" in key else RANCHER_WORKER_POOL_ID_DEFAULT
