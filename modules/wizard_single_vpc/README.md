# Wizard Single VPC

Guided setup for a Catalyst single-VPC deployment. It derives the customer from
the selected release, fixes the product as `CATALYST`, and collects common,
network, EKS, Rancher, deployment, and integration settings.

The flow is backed by a persistent user-owned workspace. Users can resume it from
**My setups**, revisit its generated spec and validation report, share it with
viewer/editor permissions, or delete it. Repository creation requires a
successful full Praxis Core validation with IaC mock preflight.

Single-VPC defaults keep Istio/Vault replacements and multi-mesh disabled while
enabling the corresponding Rancher labels. Git repository authentication
defaults to SSH.
