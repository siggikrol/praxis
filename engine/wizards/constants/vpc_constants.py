# IDs are stable across accounts; extend as needed
AZ_ZONE_IDS_BY_REGION = {
    "us-east-1": ["use1-az1", "use1-az2", "use1-az3", "use1-az4", "use1-az5", "use1-az6"],
    "eu-west-1": ["euw1-az1", "euw1-az2", "euw1-az3"],
    "eu-central-1": ["euc1-az1", "euc1-az2", "euc1-az3", "euc1-az4", "euc1-az5", "euc1-az6"],
    "ca-central-1": ["cac1-az1", "cac1-az2", "cac1-az3", "cac1-az4", "cac1-az5", "cac1-az6"],
    "ap-southeast-2": ["apse2-az1", "apse2-az2", "apse2-az3", "apse2-az4", "apse2-az5", "apse2-az6"]
}

# already have (shown for context)
VPC_AZ_PROFILE_CHOICES = [
    ("custom", "Custom"),
    ("use1-shared-latest", "US-East-1 (Latest Secured VPC)"),
    ("use1-shared-old", "US-East-1 (Old Secured VPC)"),
    ("use1-shared-loyalty", "US-East-1 (Loyalty/PlayON VPC)"),
    ("euc1-shared", "EU-Central-1 (LCAT/ST)"),
    ("euw1-shared", "EU-West-1 (NT/LNL)"),
    ("cac1-shared", "CA-Central-1 (LQ/RGS-SIT)"),
    ("apse2-shared", "AP-SouthEast-2 (TLC/ST)"),
]

VPC_AZ_PROFILES = {
    "use1-shared-latest": {
        "aws_region": "us-east-1",
        "transit_gateway_id": "tgw-04005a7f93411ea02",
        "vpc_endpoint_service": "com.amazonaws.vpce.us-east-1.vpce-svc-0c5487c4e4b856510",
        "az_names":   ["us-east-1a", "us-east-1b", "us-east-1c"],
        "az_zone_ids":["use1-az1", "use1-az2", "use1-az4"],
    },
    "use1-shared-old": {
        "aws_region": "us-east-1",
        "transit_gateway_id": "tgw-04c08b2473e6b809d",
        "vpc_endpoint_service": "com.amazonaws.vpce.us-east-1.vpce-svc-09e7e29afb47370cc",
        "az_names":   ["us-east-1c", "us-east-1b", "us-east-1a"],
        "az_zone_ids":["use1-az1", "use1-az2", "use1-az6"],
    },
    "use1-shared-loyalty": {
        "aws_region": "us-east-1",
        "transit_gateway_id": "tgw-07526f4ceee38a429",
        "vpc_endpoint_service": "com.amazonaws.vpce.us-east-1.vpce-svc-09e7e29afb47370cc",
        "az_names":   ["us-east-1c", "us-east-1b", "us-east-1a"],
        "az_zone_ids":["use1-az1", "use1-az2", "use1-az6"],
    },
    "euc1-shared": {
        "aws_region": "eu-central-1",
        "transit_gateway_id": "tgw-07800654b0c167a12",
        "vpc_endpoint_service": "com.amazonaws.vpce.eu-central-1.vpce-svc-0012086907afdf2dc",
        "az_names":   ["eu-central-1c", "eu-central-1a", "eu-central-1b"],
        "az_zone_ids":["euc1-az1", "euc1-az2", "euc1-az3"],
    },
    "euw1-shared": {
        "aws_region": "eu-west-1",
        "transit_gateway_id": "tgw-0a36c0e0c70d54953",
        "vpc_endpoint_service": "com.amazonaws.vpce.eu-west-1.vpce-svc-0eeca92b945c839ff",
        "az_names":   ["eu-west-1a", "eu-west-1b", "eu-west-1c"],
        "az_zone_ids":["euw1-az3", "euw1-az1", "euw1-az2"],
    },
    "cac1-shared": {
        "aws_region": "ca-central-1",
        "transit_gateway_id": "tgw-0aa274203fd7abd85",
        "vpc_endpoint_service": "com.amazonaws.vpce.ca-central-1.vpce-svc-03c7d1f2d86fa4ed6",
        "az_names":   ["ca-central-1a", "ca-central-1b", "ca-central-1c"],
        "az_zone_ids":["cac1-az1", "cac1-az2", "cac1-az4"],
    },
    "apse2-shared": {
        "aws_region": "ap-southeast-2",
        "transit_gateway_id": "tgw-05bf6f3338539cfdd",
        "vpc_endpoint_service": "com.amazonaws.vpce.ap-southeast-2.vpce-svc-07f78c3fe99ca3ef4",
        "az_names":   ["ap-southeast-2a", "ap-southeast-2c", "ap-southeast-2b"],
        "az_zone_ids":["apse2-az1", "apse2-az2", "apse2-az3"],
    },
}


def default_vpc_profile_for_region(region: str) -> str:
    region = (region or "").strip()
    if not region:
        return ""

    for profile_key, _label in VPC_AZ_PROFILE_CHOICES:
        profile = VPC_AZ_PROFILES.get(profile_key)
        if profile and profile.get("aws_region") == region:
            return profile_key
    return ""
