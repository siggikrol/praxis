# Wizard Multi VPC

Guided setup for Catalyst multi-VPC and multi-cluster deployments. It gathers
environment/deployment topology, replacements, networking, EKS, Rancher,
Spacelift, and deployment settings and generates one validated spec.

The flow is backed by a persistent user-owned workspace with resume, validation
history, sharing, and deletion from **My setups**. Repository creation requires a
successful full Praxis Core validation with IaC mock preflight.

Multi-VPC defaults enable Istio and Vault replacements and enable Rancher
multi-mesh. Git repository authentication defaults to SSH.
