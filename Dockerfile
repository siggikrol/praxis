###########  BUILD ###########
FROM python:3.11-slim-bookworm AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

# git is needed at build time only if you pip install from git; otherwise optional here
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN python -m pip install --upgrade pip setuptools wheel && \
    python -m pip install \
      --target=/opt/python/lib/python3.11/site-packages \
      -r requirements.txt \
      gunicorn

# app code
COPY app.py constants.py auth.py ./
COPY services/ services/
COPY modules/ modules/
COPY engine/ engine/
COPY static/ static/
COPY templates/ templates/
COPY logs/ logs/
COPY scripts/aws /usr/local/bin/aws

# byte-compile (optional)
RUN python -m compileall -q /app

# make everything world-readable and dirs traversable
RUN chmod -R a+rX /app /opt/python && chmod 755 /usr/local/bin/aws

# capture version at build
ARG RELEASE_VERSION=dev
RUN printf '%s' "$RELEASE_VERSION" > /app/.build_version
ARG GIT_SHA=dev
ENV APP_VERSION=$GIT_SHA

###########  RUNTIME ###########
FROM python:3.11-slim-bookworm

# git is used by export_repo_to_tmp(). The AWS CLI is required by EKS
# kubeconfigs, whose exec credential plugin calls `aws eks get-token`.
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates awscli && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/python/lib/python3.11/site-packages \
    AWS_EC2_METADATA_DISABLED=true \
    PS_VALIDATE_CACHES_ON_STARTUP=0 \
    PS_AWS_CATALOG_REFRESHER_ENABLED=0 \
    PS_PC_SOURCE_REFRESHER_ENABLED=0 \
    PS_RELEASES_PROVIDER=disabled 

WORKDIR /app
COPY --from=build /opt/python /opt/python
COPY --from=build /app /app
COPY --from=build /usr/local/bin/aws /usr/local/bin/aws

# run as non-root uid/gid 65532 (same as your distroless setup)
RUN useradd -u 65532 -r -s /usr/sbin/nologin appuser
USER 65532:65532

EXPOSE 5000

# IMPORTANT: use 'python' (on PATH), not '/usr/bin/python3'
ENTRYPOINT ["python","-m","gunicorn","--bind","0.0.0.0:5000","--workers","3","--threads","4","--no-control-socket","app:app"]
