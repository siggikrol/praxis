"""Source notes for the foundation-readiness networking and AWS prerequisites."""

SOURCE_DOCUMENTS = {
    "playbook": {
        "title": "06.04 Catalyst Setup Playbook",
        "url": "",
    },
    "retro": {
        "title": "Retro LNL — Environment Setup Stall Timeline",
        "url": "",
    },
    "migration": {
        "title": "LNL — Catalyst — Data Migration DEV",
        "url": "",
    },
}

ORIGIN_GROUPS = (
    {
        "key": "networking",
        "title": "Networking prerequisites",
        "items": (
            ("cidr", "CIDR and subnet allocation", "playbook", "2", "The kickoff checklist requires approved CIDR/subnet allocation; the migration record also shows the account CIDR and AZ ranges as tracked setup inputs."),
            ("firewall", "Firewall and PROD/NONPROD alignment", "playbook", "2–5", "The playbook requires firewall classification and whitelist tickets before setup. The retro records unclear firewall assumptions and missing rules as recurring delays."),
            ("tgw", "Networking-owned Transit Gateway account setup and VPC attachment", "playbook", "5 and 9", "Catalyst DEV/STG accounts were not added to the Transit Gateway share/principal. The Networking team must configure the Transit Gateway for the target AWS account and complete the environment VPC attachment before shared routing can work; this is not performed by Praxis."),
            ("vpce", "VPC endpoint service and region", "playbook", "5 and 9", "The playbook requires the endpoint service and TGW IDs and records failures caused by region/endpoint-service mismatch."),
            ("internet", "Outbound internet for VPC/EKS nodes", "retro", "2–3", "EKS nodes could not proceed because outbound internet had been omitted. The retro calls this a recurring foundation problem outside Praxis."),
            ("vpn", "VPN/office access", "playbook", "5–6", "The readiness checklist calls for VPN/office reachability and provides the Catalyst ticket pattern used to make the environment reachable from the corporate VPN."),
            ("rancher", "Rancher connectivity", "retro", "3", "The Catalyst EKS cluster could not reach Rancher Preprod, blocking application deployment until firewall/connectivity work was completed and verified."),
            ("vault", "Shared Vault connectivity", "retro", "3–8", "DEV and STG could not reach shared Vault. Helm/application work and External Secrets authentication were blocked until networking and auth state were corrected."),
            ("harbor", "Harbor endpoint access", "retro", "9–10", "DEV and STG timed out reaching Harbor, so cluster workloads could not pull images. Work resumed only after the network change was completed."),
        ),
    },
    {
        "key": "aws_platform",
        "title": "AWS account and platform prerequisites",
        "items": (
            ("aws_account", "AWS account created and accessible", "migration", "1–2", "The environment record tracks account creation as a separate request and records the target account before infrastructure work begins."),
            ("naming", "Environment naming is consistent", "retro", "1 and 6–7", "Staging C/E ambiguity and stg-versus-staging IAM naming mismatches caused incorrect roles and ClusterSecretStore failures."),
            ("iam_access", "Spacelift execution and administrator IAM access", "retro", "6–7", "The bootstrap role required manual adjustment and External Secrets failed to assume the expected IAM role. The playbook also requires IAM and Vault-auth naming checks."),
        ),
    },
    {
        "key": "conditional",
        "title": "Conditional services",
        "items": (
            ("grafana", "Grafana region and firewall access", "playbook", "3–5", "The playbook requires the target Grafana region and firewall request to be known. It explicitly says this is needed for monitoring completion, not necessarily to begin foundation provisioning."),
            ("jumphost", "Jump-host creation and access", "playbook", "3", "The kickoff checklist asks whether a jump host is needed so its ownership and access work can be planned rather than discovered during deployment."),
        ),
    },
)
