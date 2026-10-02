FROM ghcr.io/opentofu/opentofu:1.12.0-minimal AS tofu
FROM python:3.11-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates git libseccomp2 diffutils && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir 'Flask>=3,<4' 'gunicorn>=26,<27' 'python-hcl2>=7,<8'
COPY --from=tofu /usr/local/bin/tofu /usr/local/bin/tofu
WORKDIR /runner
COPY services/tofu_runner/ /runner/
USER 65532:65532
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
ENTRYPOINT ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--threads", "2", "--timeout", "420", "--no-control-socket", "server:app"]
