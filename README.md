# Praxis

Praxis is a Flask application for infrastructure specifications, provisioning wizards, environment readiness, and release workflows.

Install the dependencies from `requirements.txt` and set `FLASK_SECRET` before starting `python app.py`. Authentication and external integrations require deployment-specific configuration; see the module READMEs and `docker-compose.yml`.

The application uses Praxis branding, `praxis_*` specification keys, the `praxisrelease` module, and Praxis cache directories and Docker volumes. Existing saved specs, release manifests, Kubernetes resources, and deployment configuration must use the new identifiers. Existing volumes are not migrated automatically.

Configure your own GitHub owner/repository, S3 buckets, Spacelift endpoint, and ServiceNow instance. `DOCS_URL` optionally enables the documentation link. Example domains in networking profiles and Rancher settings are placeholders to replace before provisioning. Environment variable names beginning with `PS_` remain supported.

## Docker without AWS

Copy `.env.example` to `.env`, set a random `FLASK_SECRET` and your login password, then run:

```sh
docker compose up --build
```

Open `http://localhost` and sign in using `BASIC_USER` and `BASIC_PASSWORD`. No AWS account, credentials, local AWS directory, kubeconfig, Spacelift config, or GitHub organization is required. Startup cache refreshes and AWS metadata discovery are disabled. Release lists remain empty until an S3 integration is configured; live AWS catalog refresh, cloud validation, and provisioning require their respective integrations. Building the image still downloads public packages and base images.

To opt into AWS releases and catalog refresh, configure the variables listed in `docker-compose.aws.yml` and run:

```sh
docker compose -f docker-compose.yml -f docker-compose.aws.yml up --build
```

Kubernetes/EKS validation requires a separately configured kubeconfig mount and credentials; the default stack does not mount any host credentials.

The default proxy serves HTTP locally. Set `FLASK_SECURE_COOKIES=1` when deploying behind HTTPS.

## Environment workflow

Create Environment groups the existing forms into Environment, Cloud, Architecture, Capabilities, and Review. AWS is the available provider; Google Cloud and Azure are informational future options. Environment name, description, and owner/team are optional workspace metadata, persisted with the existing project settings and excluded from generated specifications. Existing environment type keys, AWS resource prefixes, deployment identifiers, and generation remain unchanged. Review summarizes saved configuration, not deployed infrastructure.
