from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from typing import Any

from services.github_helpers.forms import (
    DEFAULT_HARBOR_REGISTRY_PROJECT,
    DEFAULT_PC_VERSION,
)
from werkzeug.utils import safe_join
from services.spec_fingerprint import spec_fingerprint

logger = logging.getLogger(__name__)

try:
    from kubernetes import client, config
    from kubernetes.client.exceptions import ApiException
    from kubernetes.config.config_exception import ConfigException

    _HAS_K8S = True
    _K8S_IMPORT_ERROR = ""
except Exception as exc:  # pragma: no cover - import branch depends on runtime image.
    client = None  # type: ignore[assignment]
    config = None  # type: ignore[assignment]
    ApiException = Exception  # type: ignore[assignment]
    ConfigException = Exception  # type: ignore[assignment]
    _HAS_K8S = False
    logger.exception("Failed to import kubernetes client libraries")
    _K8S_IMPORT_ERROR = "import error"


JOB_ID_RE = re.compile(r"^[a-f0-9]{32}$")
PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


# This script runs inside the selected Praxis Core image. Legacy images do not
# expose praxis-version, while Core v2+ advertises its supported validation
# contract as JSON. Keeping the dispatch here lets Studio support both image layouts
# without guessing from mutable image tags.
VALIDATION_DISPATCH_SCRIPT = r'''from __future__ import annotations

import json
import os
import subprocess
import sys


def fail(message: str) -> "NoReturn":
    print(f"Praxis Core validation dispatch failed: {message}", file=sys.stderr)
    raise SystemExit(2)


spec_path, legacy_script, validation_mode, iac_test_raw = sys.argv[1:5]
if validation_mode not in {"offline", "full"}:
    fail(f"unsupported validation mode {validation_mode!r}")
run_iac_test = iac_test_raw == "1"
if run_iac_test and validation_mode != "full":
    fail("IAC mock preflight requires full validation mode")
try:
    probe = subprocess.run(
        ["praxis-version", "--json"],
        check=False,
        capture_output=True,
        text=True,
    )
except FileNotFoundError:
    if run_iac_test:
        fail("IAC mock preflight requires a modern Praxis Core image")
    print(
        f"Detected legacy Praxis Core validation API ({validation_mode} mode)",
        file=sys.stderr,
    )
    legacy_args = ["python3", legacy_script, spec_path]
    if validation_mode == "offline":
        legacy_args.append("--dryrun")
    os.execvp("python3", legacy_args)

if probe.returncode != 0:
    fail(f"praxis-version exited with status {probe.returncode}")

try:
    metadata = json.loads(probe.stdout)
except (TypeError, ValueError) as exc:
    fail(f"praxis-version returned invalid JSON: {exc}")

if not isinstance(metadata, dict):
    fail("praxis-version JSON must be an object")

validation_api = metadata.get("validation_api")
validation_command = str(metadata.get("validation_command") or "").strip()
core_version = str(metadata.get("core_version") or "unknown").strip()
if validation_api != 2:
    fail(f"unsupported validation API {validation_api!r}")
if validation_command != "praxis-validate":
    fail(f"unsupported validation command {validation_command!r}")

print(
    f"Detected Praxis Core {core_version} validation API {validation_api} "
    f"({validation_mode} mode) iac_test={str(run_iac_test).lower()}",
    file=sys.stderr,
)
if run_iac_test:
    try:
        generate_help = subprocess.run(
            ["praxis-generate", "--help"],
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        fail("praxis-generate is unavailable in the selected Core image")
    if generate_help.returncode != 0 or "--iac-test" not in generate_help.stdout:
        fail("selected Praxis Core image does not support --iac-test")
    os.execvp(
        "praxis-generate",
        ["praxis-generate", spec_path, "--iac-test"],
    )

validation_args = [validation_command, spec_path]
if validation_mode == "offline":
    for name in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_PROFILE",
    ):
        os.environ.pop(name, None)
else:
    validation_args.append("--full")
os.execvp(validation_command, validation_args)
'''


def _sha256_text(text: str) -> str:
    return spec_fingerprint(text)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int, min_value: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(min_value, value)


def _env_float(name: str, default: float, min_value: float) -> float:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return max(min_value, value)


def _split_csv(raw: str | None) -> list[str]:
    return [p.strip() for p in (raw or "").split(",") if p.strip()]


def _job_dir() -> str:
    base = (
        os.getenv("PS_CORE_VALIDATE_JOB_DIR")
        or os.getenv("PS_PC_SOURCE_CACHE_DIR")
        or "/tmp/praxis-cache"
    )
    return os.path.join(base.rstrip("/"), "core-validate-jobs")


def _normalize_job_id(job_id: str) -> str:
    normalized = (job_id or "").strip()
    if not JOB_ID_RE.fullmatch(normalized):
        raise ValueError("Invalid core validate job id")
    return normalized


def _job_path(job_id: str) -> str:
    safe_job_id = _normalize_job_id(job_id)
    path = safe_join(_job_dir(), f"{safe_job_id}.json")
    if not path:
        raise ValueError("Invalid core validate job path")
    return path


def _read_job(job_id: str) -> dict[str, Any] | None:
    try:
        path = _job_path(job_id)
    except ValueError:
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except FileNotFoundError:
        return None
    except Exception:
        return None


def _write_job(job_id: str, payload: dict[str, Any]) -> None:
    path = _job_path(job_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp.{uuid.uuid4().hex}"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


def get_job(job_id: str) -> dict[str, Any] | None:
    return _read_job(job_id)


def get_latest_job(env_slug: str) -> dict[str, Any] | None:
    """Return the newest persisted validation job for a wizard environment."""
    wanted_env = (env_slug or "").strip()
    if not wanted_env:
        return None

    try:
        names = os.listdir(_job_dir())
    except (FileNotFoundError, OSError):
        return None

    latest: dict[str, Any] | None = None
    latest_created = -1.0
    for name in names:
        match = re.fullmatch(r"([a-f0-9]{32})\.json", name)
        if not match:
            continue
        job = _read_job(match.group(1))
        if not job or str(job.get("env_slug") or "") != wanted_env:
            continue
        try:
            created = float(job.get("created_at_epoch") or 0)
        except (TypeError, ValueError):
            created = 0.0
        if latest is None or created > latest_created:
            latest = job
            latest_created = created
    return latest


def _append_event(
    job: dict[str, Any],
    *,
    level: str,
    message: str,
    step: str | None = None,
) -> None:
    ev = {
        "ts_epoch": time.time(),
        "level": level,
        "message": message,
        "step": step,
    }
    events = job.get("events")
    if not isinstance(events, list):
        events = []
        job["events"] = events
    events.append(ev)


def _set_state(job: dict[str, Any], state: str) -> None:
    job["state"] = state


def _feature_enabled() -> bool:
    return _env_bool("PS_CORE_VALIDATE_ENABLED", True)


def _registry_address() -> str:
    addr = (
        os.getenv("PS_CORE_VALIDATE_REGISTRY_ADDRESS")
        or os.getenv("PS_HARBOR_REGISTRY_ADDRESS")
        or os.getenv("HARBOR_REGISTRY_ADDRESS")
        or ""
    )
    return addr.strip().rstrip("/")


def _runtime_namespace() -> str:
    explicit = (os.getenv("PS_CORE_VALIDATE_NAMESPACE") or "").strip()
    if explicit:
        return explicit
    pod_ns = (os.getenv("POD_NAMESPACE") or "").strip()
    if pod_ns:
        return pod_ns
    sa_path = "/var/run/secrets/kubernetes.io/serviceaccount/namespace"
    try:
        with open(sa_path, "r", encoding="utf-8") as fh:
            val = fh.read().strip()
        if val:
            return val
    except Exception:
        pass
    return "default"


def _image_for(project: str, version: str) -> str:
    return f"{_registry_address()}/{project}/praxis-core:{version}"


def feature_state() -> dict[str, Any]:
    defaults = {
        "harbor_registry_project": (
            os.getenv("PS_CORE_VALIDATE_DEFAULT_HARBOR_PROJECT")
            or DEFAULT_HARBOR_REGISTRY_PROJECT
        ).strip(),
        "pc_version": (
            os.getenv("PS_CORE_VALIDATE_DEFAULT_PC_VERSION")
            or DEFAULT_PC_VERSION
        ).strip(),
    }
    state: dict[str, Any] = {
        "enabled": False,
        "reason": "",
        "defaults": defaults,
        "registry_address": _registry_address(),
        "namespace": _runtime_namespace(),
    }
    if not _feature_enabled():
        state["reason"] = "disabled by PS_CORE_VALIDATE_ENABLED"
        return state
    if not _HAS_K8S:
        state["reason"] = f"kubernetes client unavailable ({_K8S_IMPORT_ERROR})"
        return state
    if not state["registry_address"]:
        state["reason"] = "missing registry address (set PS_CORE_VALIDATE_REGISTRY_ADDRESS)"
        return state
    state["enabled"] = True
    return state


def _validate_inputs(
    *,
    env_slug: str,
    spec_yaml: str,
    harbor_registry_project: str,
    pc_version: str,
) -> None:
    if not (env_slug or "").strip():
        raise ValueError("Missing env_slug")
    if not isinstance(spec_yaml, str) or not spec_yaml.strip():
        raise ValueError("Spec YAML is empty")
    if len(spec_yaml.encode("utf-8")) > 900_000:
        raise ValueError("Spec YAML is too large for ConfigMap transport")
    if not PROJECT_RE.fullmatch(harbor_registry_project or ""):
        raise ValueError(
            "Invalid harbor project (expected lowercase, digits, dot, underscore, hyphen)"
        )
    if not VERSION_RE.fullmatch(pc_version or ""):
        raise ValueError(
            (
                "Invalid Praxis Core version tag "
                "(allowed: letters, digits, dot, underscore, hyphen)"
            )
        )


def _load_k8s_config() -> str:
    assert config is not None
    try:
        config.load_incluster_config()
        return "incluster"
    except ConfigException:
        kubeconfig = (os.getenv("KUBECONFIG") or "").strip()
        config.load_kube_config(config_file=(kubeconfig or None))
        return "kubeconfig"


def _pod_for_job(core_api: Any, namespace: str, job_name: str) -> tuple[str, str]:
    pods = core_api.list_namespaced_pod(
        namespace=namespace,
        label_selector=f"job-name={job_name}",
    )
    items = list((pods.items or []))
    if not items:
        return "", ""

    def _created_epoch(pod: Any) -> float:
        ts = getattr(getattr(pod, "metadata", None), "creation_timestamp", None)
        try:
            return float(ts.timestamp()) if ts else 0.0
        except Exception:
            return 0.0

    items.sort(key=_created_epoch, reverse=True)
    pod = items[0]
    pod_name = (pod.metadata.name or "").strip()
    phase = (pod.status.phase or "").strip()
    return pod_name, phase


def _trim_text(s: str, max_chars: int) -> str:
    if len(s) <= max_chars:
        return s
    marker = "\n\n...[log truncated: showing start and end]...\n\n"
    if max_chars <= (len(marker) + 40):
        return s[:max_chars]
    head = (max_chars - len(marker)) // 2
    tail = max_chars - len(marker) - head
    return f"{s[:head]}{marker}{s[-tail:]}"


def _capture_pod_logs(
    core_api: Any,
    *,
    namespace: str,
    pod_name: str,
    max_log_chars: int,
    tail_lines: int | None = None,
) -> str:
    if not pod_name:
        return ""
    kwargs: dict[str, Any] = {
        "name": pod_name,
        "namespace": namespace,
        "timestamps": True,
    }
    if tail_lines is not None:
        kwargs["tail_lines"] = tail_lines
    try:
        logs = core_api.read_namespaced_pod_log(**kwargs)
    except ApiException as exc:
        if exc.status in (400, 404):
            return ""
        raise
    return _trim_text(logs or "", max_log_chars)


def _pod_failure_detail(core_api: Any, *, namespace: str, pod_name: str) -> str:
    if not pod_name:
        return ""
    try:
        pod = core_api.read_namespaced_pod(name=pod_name, namespace=namespace)
    except ApiException as exc:
        if exc.status in (400, 404):
            return ""
        raise

    status = getattr(pod, "status", None)
    if status is None:
        return ""

    details: list[str] = []
    container_statuses = list(getattr(status, "container_statuses", None) or [])
    for cs in container_statuses:
        name = (getattr(cs, "name", None) or "container").strip()
        state = getattr(cs, "state", None)
        terminated = getattr(state, "terminated", None) if state is not None else None
        waiting = getattr(state, "waiting", None) if state is not None else None

        if terminated is not None:
            exit_code = getattr(terminated, "exit_code", None)
            reason = (getattr(terminated, "reason", None) or "").strip()
            message = (getattr(terminated, "message", None) or "").strip()
            parts = [f"{name}: terminated"]
            if exit_code is not None:
                parts.append(f"exit_code={exit_code}")
            if reason:
                parts.append(f"reason={reason}")
            if message:
                parts.append(f"message={message}")
            details.append(", ".join(parts))
            continue

        if waiting is not None:
            reason = (getattr(waiting, "reason", None) or "").strip()
            message = (getattr(waiting, "message", None) or "").strip()
            parts = [f"{name}: waiting"]
            if reason:
                parts.append(f"reason={reason}")
            if message:
                parts.append(f"message={message}")
            details.append(", ".join(parts))

    if details:
        merged = "; ".join(details)
        return merged[:800] if len(merged) > 800 else merged

    phase = (getattr(status, "phase", None) or "").strip()
    reason = (getattr(status, "reason", None) or "").strip()
    message = (getattr(status, "message", None) or "").strip()
    pod_parts = []
    if phase:
        pod_parts.append(f"phase={phase}")
    if reason:
        pod_parts.append(f"reason={reason}")
    if message:
        pod_parts.append(f"message={message}")
    return ", ".join(pod_parts)


def _add_aws_env(env_vars: list[Any]) -> None:
    assert client is not None
    secret_name = (os.getenv("PS_CORE_VALIDATE_AWS_SECRET_NAME") or "").strip()
    access_key_key = (
        os.getenv("PS_CORE_VALIDATE_AWS_ACCESS_KEY_ID_KEY") or "AWS_ACCESS_KEY_ID"
    ).strip()
    secret_key_key = (
        os.getenv("PS_CORE_VALIDATE_AWS_SECRET_ACCESS_KEY_KEY") or "AWS_SECRET_ACCESS_KEY"
    ).strip()
    session_key_key = (
        os.getenv("PS_CORE_VALIDATE_AWS_SESSION_TOKEN_KEY") or "AWS_SESSION_TOKEN"
    ).strip()

    if secret_name:
        env_vars.append(
            client.V1EnvVar(
                name="AWS_ACCESS_KEY_ID",
                value_from=client.V1EnvVarSource(
                    secret_key_ref=client.V1SecretKeySelector(
                        name=secret_name,
                        key=access_key_key,
                    )
                ),
            )
        )
        env_vars.append(
            client.V1EnvVar(
                name="AWS_SECRET_ACCESS_KEY",
                value_from=client.V1EnvVarSource(
                    secret_key_ref=client.V1SecretKeySelector(
                        name=secret_name,
                        key=secret_key_key,
                    )
                ),
            )
        )
        if session_key_key:
            env_vars.append(
                client.V1EnvVar(
                    name="AWS_SESSION_TOKEN",
                    value_from=client.V1EnvVarSource(
                        secret_key_ref=client.V1SecretKeySelector(
                            name=secret_name,
                            key=session_key_key,
                            optional=True,
                        )
                    ),
                )
            )
        return

    # Fallback: pass through current process env values when available.
    if os.getenv("AWS_ACCESS_KEY_ID"):
        env_vars.append(
            client.V1EnvVar(
                name="AWS_ACCESS_KEY_ID",
                value=os.getenv("AWS_ACCESS_KEY_ID"),
            )
        )
    if os.getenv("AWS_SECRET_ACCESS_KEY"):
        env_vars.append(
            client.V1EnvVar(name="AWS_SECRET_ACCESS_KEY", value=os.getenv("AWS_SECRET_ACCESS_KEY"))
        )
    if os.getenv("AWS_SESSION_TOKEN"):
        env_vars.append(
            client.V1EnvVar(
                name="AWS_SESSION_TOKEN",
                value=os.getenv("AWS_SESSION_TOKEN"),
            )
        )


def _add_tf_registry_env(env_vars: list[Any]) -> None:
    """Pass the private Spacelift module-registry token to validation Jobs."""
    assert client is not None
    env_name = (
        os.getenv("PS_CORE_VALIDATE_TF_TOKEN_ENV_NAME")
        or "TF_TOKEN_spacelift_io"
    ).strip()
    secret_name = (
        os.getenv("PS_CORE_VALIDATE_TF_TOKEN_SECRET_NAME") or ""
    ).strip()
    secret_key = (
        os.getenv("PS_CORE_VALIDATE_TF_TOKEN_SECRET_KEY")
        or env_name
    ).strip()

    if secret_name:
        env_vars.append(
            client.V1EnvVar(
                name=env_name,
                value_from=client.V1EnvVarSource(
                    secret_key_ref=client.V1SecretKeySelector(
                        name=secret_name,
                        key=secret_key,
                    )
                ),
            )
        )
        return

    token = os.getenv(env_name)
    if token:
        env_vars.append(client.V1EnvVar(name=env_name, value=token))


def start_validation_job(
    *,
    env_slug: str,
    spec_yaml: str,
    harbor_registry_project: str,
    pc_version: str,
    validation_mode: str = "offline",
    run_iac_test: bool = False,
) -> dict[str, Any]:
    state = feature_state()
    if not state.get("enabled"):
        raise RuntimeError(state.get("reason") or "Core validation is not available")

    env_slug = (env_slug or "").strip()
    harbor_registry_project = (harbor_registry_project or "").strip()
    pc_version = (pc_version or "").strip()
    validation_mode = (validation_mode or "").strip().lower()
    if validation_mode not in {"offline", "full"}:
        raise ValueError("Invalid core validation mode")
    run_iac_test = bool(run_iac_test)
    if validation_mode == "offline" and run_iac_test:
        raise ValueError("IAC mock preflight requires full validation mode")
    _validate_inputs(
        env_slug=env_slug,
        spec_yaml=spec_yaml,
        harbor_registry_project=harbor_registry_project,
        pc_version=pc_version,
    )

    job_id = uuid.uuid4().hex
    image = _image_for(harbor_registry_project, pc_version)
    payload: dict[str, Any] = {
        "job_id": job_id,
        "env_slug": env_slug,
        "spec_hash": _sha256_text(spec_yaml),
        "spec_hash_kind": "yaml-semantic-v1",
        "created_at_epoch": time.time(),
        "started_at_epoch": None,
        "finished_at_epoch": None,
        "state": "queued",  # queued | running | done | error
        "ok": None,
        "events": [],
        "error": None,
        "log_tail": "",
        "total_steps": 4,
        "completed_steps": 0,
        "image": image,
        "harbor_registry_project": harbor_registry_project,
        "pc_version": pc_version,
        "validation_mode": validation_mode,
        "run_iac_test": run_iac_test,
        "k8s": {
            "namespace": _runtime_namespace(),
            "job_name": "",
            "pod_name": "",
            "config_source": "",
        },
    }
    _append_event(
        payload,
        level="info",
        message=(
            f"Queued Praxis Core {validation_mode} validation "
            f"(IAC mock preflight {'enabled' if run_iac_test else 'disabled'})"
        ),
    )
    _write_job(job_id, payload)

    cfg = {
        "env_slug": env_slug,
        "spec_yaml": spec_yaml,
        "image": image,
        "pc_version": pc_version,
        "harbor_registry_project": harbor_registry_project,
        "validation_mode": validation_mode,
        "run_iac_test": run_iac_test,
    }

    t = threading.Thread(
        target=_run_validation_job,
        name=f"core-validate:{job_id}",
        args=(job_id, cfg),
        daemon=True,
    )
    t.start()
    return {"ok": True, "job_id": job_id, "state": "queued"}


def _set_step(job: dict[str, Any], job_id: str, completed_steps: int) -> None:
    total = int(job.get("total_steps") or 0)
    completed = max(0, min(total if total > 0 else completed_steps, completed_steps))
    job["completed_steps"] = completed
    _write_job(job_id, job)


def _run_validation_job(job_id: str, cfg: dict[str, Any]) -> None:
    job_id = _normalize_job_id(job_id)
    job = _read_job(job_id) or {}
    namespace = _runtime_namespace()
    poll_seconds = _env_float("PS_CORE_VALIDATE_POLL_SECONDS", 1.2, 0.3)
    timeout_seconds = _env_int("PS_CORE_VALIDATE_JOB_TIMEOUT_SECONDS", 900, 60)
    ttl_seconds = _env_int("PS_CORE_VALIDATE_JOB_TTL_SECONDS", 600, 60)
    tail_lines = _env_int("PS_CORE_VALIDATE_LOG_TAIL_LINES", 1200, 100)
    max_log_chars = _env_int("PS_CORE_VALIDATE_MAX_LOG_CHARS", 250000, 5000)
    service_account = (os.getenv("PS_CORE_VALIDATE_JOB_SERVICE_ACCOUNT") or "").strip()
    pull_secrets = _split_csv(os.getenv("PS_CORE_VALIDATE_IMAGE_PULL_SECRETS"))
    script_path = (
        os.getenv("PS_CORE_VALIDATE_SCRIPT_PATH")
        or "/praxis/environments/core_generator.py"
    ).strip()

    job_name = f"pc-core-validate-{job_id[:8]}"
    cm_name = f"pc-core-spec-{job_id[:8]}"
    image = cfg.get("image") or ""

    try:
        job["started_at_epoch"] = time.time()
        _set_state(job, "running")
        _append_event(job, level="info", message="Validation job started", step="bootstrap")
        job.setdefault("k8s", {})
        job["k8s"]["namespace"] = namespace
        job["k8s"]["job_name"] = job_name
        _write_job(job_id, job)
        _set_step(job, job_id, 1)

        config_source = _load_k8s_config()
        job["k8s"]["config_source"] = config_source
        _append_event(
            job,
            level="info",
            message=f"Kubernetes config loaded ({config_source})",
            step="bootstrap",
        )
        _write_job(job_id, job)

        assert client is not None
        core_api = client.CoreV1Api()
        batch_api = client.BatchV1Api()

        labels = {
            "app.kubernetes.io/name": "praxis",
            "app.kubernetes.io/component": "core-validate",
            "praxis.dev/env-slug": str(cfg.get("env_slug") or "").strip()[:63],
            "praxis.dev/job-id": job_id[:32],
        }

        cfg_map = client.V1ConfigMap(
            metadata=client.V1ObjectMeta(
                name=cm_name,
                labels=labels,
            ),
            data={
                "spec.yaml": str(cfg.get("spec_yaml") or ""),
                "validate_dispatch.py": VALIDATION_DISPATCH_SCRIPT,
            },
        )
        core_api.create_namespaced_config_map(namespace=namespace, body=cfg_map)
        _append_event(
            job,
            level="info",
            message=f"ConfigMap created ({cm_name})",
            step="prepare",
        )
        _write_job(job_id, job)
        _set_step(job, job_id, 2)

        env_vars = [
            client.V1EnvVar(name="HOME", value="/tmp"),
            client.V1EnvVar(name="PC_OUT_DIR", value="/tmp/pc-generated"),
        ]
        _add_aws_env(env_vars)
        if cfg.get("run_iac_test"):
            _add_tf_registry_env(env_vars)

        volume_mounts = [
            client.V1VolumeMount(
                name="spec",
                mount_path="/praxis/env_templates/spec.yaml",
                sub_path="spec.yaml",
                read_only=True,
            ),
            client.V1VolumeMount(
                name="spec",
                mount_path="/tmp/praxis-validate.py",
                sub_path="validate_dispatch.py",
                read_only=True,
            ),
        ]
        volumes = [
            client.V1Volume(
                name="spec",
                config_map=client.V1ConfigMapVolumeSource(
                    name=cm_name,
                    items=[
                        client.V1KeyToPath(key="spec.yaml", path="spec.yaml"),
                        client.V1KeyToPath(
                            key="validate_dispatch.py",
                            path="validate_dispatch.py",
                        ),
                    ],
                ),
            )
        ]

        container = client.V1Container(
            name="core-validator",
            image=image,
            image_pull_policy=(
                os.getenv("PS_CORE_VALIDATE_IMAGE_PULL_POLICY") or "IfNotPresent"
            ).strip(),
            command=[
                "python3",
                "/tmp/praxis-validate.py",
                "/praxis/env_templates/spec.yaml",
                script_path,
                str(cfg.get("validation_mode") or "offline"),
                "1" if cfg.get("run_iac_test") else "0",
            ],
            env=env_vars,
            volume_mounts=volume_mounts,
        )

        image_pull_secret_refs = [
            client.V1LocalObjectReference(name=s) for s in pull_secrets
        ]
        pod_spec = client.V1PodSpec(
            restart_policy="Never",
            containers=[container],
            volumes=volumes,
            service_account_name=(service_account or None),
            image_pull_secrets=image_pull_secret_refs or None,
        )

        pod_template = client.V1PodTemplateSpec(
            metadata=client.V1ObjectMeta(labels=labels),
            spec=pod_spec,
        )
        job_spec = client.V1JobSpec(
            template=pod_template,
            backoff_limit=0,
            ttl_seconds_after_finished=ttl_seconds,
            active_deadline_seconds=timeout_seconds,
        )
        job_body = client.V1Job(
            api_version="batch/v1",
            kind="Job",
            metadata=client.V1ObjectMeta(name=job_name, labels=labels),
            spec=job_spec,
        )
        batch_api.create_namespaced_job(namespace=namespace, body=job_body)
        _append_event(
            job,
            level="info",
            message=f"Kubernetes Job created ({job_name})",
            step="run",
        )
        _write_job(job_id, job)
        _set_step(job, job_id, 3)

        last_pod_name = ""
        last_phase = ""
        deadline = time.time() + timeout_seconds + 30

        while time.time() < deadline:
            job_obj = batch_api.read_namespaced_job(name=job_name, namespace=namespace)
            status = job_obj.status
            succeeded = int((status.succeeded or 0) if status else 0)
            failed = int((status.failed or 0) if status else 0)
            active = int((status.active or 0) if status else 0)

            pod_name, phase = _pod_for_job(core_api, namespace, job_name)
            if pod_name:
                job["k8s"]["pod_name"] = pod_name
                if pod_name != last_pod_name:
                    _append_event(
                        job,
                        level="info",
                        message=f"Pod attached ({pod_name})",
                        step="run",
                    )
                    last_pod_name = pod_name

                if phase and phase != last_phase:
                    _append_event(
                        job,
                        level="info",
                        message=f"Pod phase={phase}",
                        step="run",
                    )
                    last_phase = phase

                trimmed = _capture_pod_logs(
                    core_api,
                    namespace=namespace,
                    pod_name=pod_name,
                    max_log_chars=max_log_chars,
                    tail_lines=tail_lines,
                )
                if trimmed != (job.get("log_tail") or ""):
                    job["log_tail"] = trimmed

            job["k8s"]["job_status"] = {
                "succeeded": succeeded,
                "failed": failed,
                "active": active,
            }
            _write_job(job_id, job)

            if succeeded > 0:
                final_logs = _capture_pod_logs(
                    core_api,
                    namespace=namespace,
                    pod_name=(job.get("k8s", {}).get("pod_name") or last_pod_name),
                    max_log_chars=max_log_chars,
                    tail_lines=None,
                )
                if final_logs:
                    job["log_tail"] = final_logs
                job["ok"] = True
                job["error"] = None
                _set_state(job, "done")
                _append_event(
                    job,
                    level="info",
                    message="Praxis Core validation succeeded",
                    step="complete",
                )
                break

            if failed > 0:
                final_logs = _capture_pod_logs(
                    core_api,
                    namespace=namespace,
                    pod_name=(job.get("k8s", {}).get("pod_name") or last_pod_name),
                    max_log_chars=max_log_chars,
                    tail_lines=None,
                )
                if final_logs:
                    job["log_tail"] = final_logs
                fail_msg = "Praxis Core validation failed"
                conds = list((status.conditions or []) if status else [])
                for c in conds:
                    if (c.type or "").lower() == "failed":
                        if c.message:
                            fail_msg = c.message
                        break
                pod_detail = _pod_failure_detail(
                    core_api,
                    namespace=namespace,
                    pod_name=(job.get("k8s", {}).get("pod_name") or last_pod_name),
                )
                if pod_detail:
                    fail_msg = f"{fail_msg} | {pod_detail}"
                job["ok"] = False
                job["error"] = fail_msg
                _set_state(job, "done")
                _append_event(
                    job,
                    level="warning",
                    message=fail_msg,
                    step="complete",
                )
                break

            time.sleep(poll_seconds)
        else:
            job["ok"] = False
            job["error"] = "Timed out waiting for validation job to finish"
            _set_state(job, "error")
            _append_event(
                job,
                level="error",
                message=job["error"],
                step="complete",
            )
            try:
                batch_api.delete_namespaced_job(
                    name=job_name,
                    namespace=namespace,
                    propagation_policy="Background",
                )
            except ApiException:
                pass

        _set_step(job, job_id, 4)

        # Remove config map as soon as processing is done; spec content is no longer needed.
        try:
            core_api.delete_namespaced_config_map(name=cm_name, namespace=namespace)
            _append_event(
                job,
                level="info",
                message=f"ConfigMap cleaned up ({cm_name})",
                step="cleanup",
            )
        except ApiException as exc:
            if exc.status != 404:
                _append_event(
                    job,
                    level="warning",
                    message=f"ConfigMap cleanup failed: {exc.reason}",
                    step="cleanup",
                )

    except Exception as exc:
        job = _read_job(job_id) or job
        job["ok"] = False
        job["error"] = str(exc)
        _set_state(job, "error")
        _append_event(job, level="error", message=f"Validation job crashed: {exc}", step="complete")
        _set_step(job, job_id, 4)
    finally:
        job = _read_job(job_id) or job
        job["finished_at_epoch"] = time.time()
        _write_job(job_id, job)
