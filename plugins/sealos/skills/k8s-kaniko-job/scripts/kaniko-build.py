#!/usr/bin/env python3
"""Build a container image with an in-cluster Kaniko Job and push it to GHCR.

For sandboxes without a Docker daemon (Brain managed deploys). The build
context is tarred into the DevBox-local VersityGW S3 store; a Kaniko Job in
the current namespace pulls it from there, builds for linux/amd64, pushes the
tagged image to ghcr.io, and reports the immutable digest.

Standard library only. Requires: kubectl, tar, GITHUB_TOKEN (write:packages).

Inputs, in precedence order:
  1. CLI flags
  2. .sealos/build-runtime.json (written by the Brain control plane):
     s3Endpoint (Job-reachable), bucket, accessKeyId, secretKeyRef
     {name,key}, buildDeadlineAt, buildDeadlineSeconds, devboxName
  3. DevBox runtime env: KANIKO_CONTEXT_POSIX_DIR, KANIKO_CONTEXT_S3_BUCKET,
     KANIKO_CONTEXT_S3_PREFIX, KANIKO_JOB_S3_ENDPOINT, S3_ENDPOINT,
     AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY / SEALOS_DEVBOX_JWT_SECRET /
     DEVBOX_JWT_SECRET, AWS_REGION, VERSITYGW_ROOT, SEALOS_DEVBOX_NAME
  4. Documented defaults (labring-actions/devbox-runtime sandbox/v1)

Usage:
  kaniko-build.py --image ghcr.io/<owner>/<repo>:<tag> [--context DIR]
                  [--dockerfile PATH] [--namespace NS] [--build-arg K=V ...]
                  [--runtime-file PATH] [--render-only] [--timeout SECONDS]
                  [--memory-limit Q] [--cpu-limit Q] [--ephemeral-limit Q]

Resources: the Job's requests/limits are sized to the namespace ResourceQuota
(remaining = hard - used, tightest quota wins), never above the defaults and
never below the floors. An explicit --*-limit is used verbatim and must fit.

Waiting: the Job is polled (not `kubectl wait`) and the build fails fast on
FailedCreate events, a pod Unschedulable for > PENDING_GRACE_SECONDS, an
ErrImagePull/ImagePullBackOff/CreateContainerError kaniko container, a Failed
pod, or a Failed Job condition.

Output: one JSON object on stdout. Progress and diagnostics go to stderr.
Success: {"success": true, "image": "<tag ref>", "digest": "sha256:...",
          "image_ref": "<repo>@sha256:...", "pull": "anonymous|private|indeterminate", ...}
"""

import argparse
import base64
import calendar
import decimal
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

DEFAULT_KANIKO_IMAGE = "gcr.io/kaniko-project/executor:v1.24.0"
DEFAULT_PLATFORM = "linux/amd64"
MAX_BUILD_SECONDS = 1800
WAIT_SLACK_SECONDS = 60
DEFAULT_BUCKET = "kaniko-contexts"
DEFAULT_PREFIX = "contexts"
DEFAULT_S3_PORT = "1319"
TAR_EXCLUDES = [".git", ".sealos", ".versitygw-s3", ".versitygw-iam", ".versitygw-versioning"]
DNS_LABEL_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
DIGEST_RE = re.compile(r"sha256:[a-f0-9]{64}")

POLL_INTERVAL_SECONDS = 5
PENDING_GRACE_SECONDS = 90
# Kubelet-side container states that never resolve on their own.
FATAL_WAITING_REASONS = frozenset(
    {
        "ErrImagePull",
        "ImagePullBackOff",
        "InvalidImageName",
        "CreateContainerError",
        "CreateContainerConfigError",
    }
)

RESOURCE_DIMENSIONS = ("cpu", "memory", "ephemeral-storage")
# Ceilings are the historical hard-coded values; floors are the smallest Job
# that still stands a chance of finishing a typical kaniko build.
RESOURCE_DEFAULTS = {
    "cpu": {"request": "500m", "limit": "2", "request_floor": "100m", "limit_floor": "500m"},
    "memory": {"request": "2Gi", "limit": "8Gi", "request_floor": "512Mi", "limit_floor": "1Gi"},
    "ephemeral-storage": {
        "request": "2Gi", "limit": "10Gi", "request_floor": "1Gi", "limit_floor": "2Gi",
    },
}


def log(message):
    print(message, file=sys.stderr, flush=True)


def fail(message, **extra):
    print(json.dumps({"success": False, "error": message, **extra}))
    sys.exit(1)


def run(cmd, input_text=None, timeout=120):
    """Run a command; return (returncode, stdout, stderr)."""
    try:
        proc = subprocess.run(
            cmd, input=input_text, capture_output=True, text=True, timeout=timeout
        )
        return proc.returncode, proc.stdout, proc.stderr
    except FileNotFoundError:
        return 127, "", f"{cmd[0]}: command not found"
    except subprocess.TimeoutExpired:
        return 124, "", f"{' '.join(cmd[:4])}...: timed out after {timeout}s"


def kubectl(args, input_text=None, timeout=120):
    return run(["kubectl", *args], input_text=input_text, timeout=timeout)


def http_json(url, headers=None, method="GET", timeout=30):
    req = urllib.request.Request(url, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), json.loads(resp.read().decode() or "null")
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        try:
            parsed = json.loads(body)
        except ValueError:
            parsed = body[:300]
        return e.code, dict(e.headers), parsed
    except (urllib.error.URLError, TimeoutError) as e:
        return 0, {}, str(e)


# ── contract resolution ─────────────────────────────────


def load_runtime_contract(path):
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except ValueError:
        log(f"warning: {path} is not valid JSON; ignoring")
        return {}


def resolve_namespace(args_namespace):
    for candidate, source in [
        (args_namespace, "flag"),
        (os.environ.get("SEALAI_NAMESPACE"), "SEALAI_NAMESPACE"),
        (os.environ.get("NAMESPACE"), "NAMESPACE"),
    ]:
        if candidate:
            return candidate.strip(), source
    code, out, _ = kubectl(
        ["config", "view", "--minify", "-o", "jsonpath={.contexts[0].context.namespace}"]
    )
    if code == 0 and out.strip():
        return out.strip(), "kube-context"
    sa_path = "/var/run/secrets/kubernetes.io/serviceaccount/namespace"
    if os.path.exists(sa_path):
        with open(sa_path) as f:
            value = f.read().strip()
        if value:
            return value, "serviceaccount"
    fail("cannot resolve namespace: pass --namespace or set NAMESPACE")


def resolve_service_account(namespace):
    pod = os.environ.get("HOSTNAME", "")
    if pod:
        code, out, _ = kubectl(
            ["get", "pod", pod, "-n", namespace, "-o", "jsonpath={.spec.serviceAccountName}"]
        )
        if code == 0 and out.strip():
            return out.strip()
    return None


def resolve_pod_ip(namespace):
    if os.environ.get("POD_IP"):
        return os.environ["POD_IP"]
    pod = os.environ.get("HOSTNAME", "")
    if pod:
        code, out, _ = kubectl(
            ["get", "pod", pod, "-n", namespace, "-o", "jsonpath={.status.podIP}"]
        )
        if code == 0 and out.strip():
            return out.strip()
    return None


def is_loopback(url):
    host = urllib.parse.urlparse(url).hostname
    return host in ("127.0.0.1", "localhost", "::1")


def resolve_job_s3_endpoint(runtime, namespace):
    """S3 endpoint the Kaniko Job Pod can reach (never loopback)."""
    for candidate in (runtime.get("s3Endpoint"), os.environ.get("KANIKO_JOB_S3_ENDPOINT")):
        if candidate and not is_loopback(candidate):
            return candidate.rstrip("/")
    local = (
        os.environ.get("S3_ENDPOINT")
        or os.environ.get("AWS_ENDPOINT_URL_S3")
        or os.environ.get("AWS_ENDPOINT_URL")
        or f"http://127.0.0.1:{DEFAULT_S3_PORT}"
    )
    if not is_loopback(local):
        return local.rstrip("/")
    pod_ip = resolve_pod_ip(namespace)
    if not pod_ip:
        fail(
            "cannot derive a Job-reachable S3 endpoint: runtime contract has no "
            "s3Endpoint, KANIKO_JOB_S3_ENDPOINT is unset, and the Pod IP is unknown"
        )
    parsed = urllib.parse.urlparse(local)
    return f"{parsed.scheme}://{pod_ip}:{parsed.port or DEFAULT_S3_PORT}"


def resolve_context_store(runtime):
    """Where to write the context tarball, and the bucket/prefix for the URI.

    The POSIX dir and the S3 URI must name the same bucket. When the runtime
    env provides KANIKO_CONTEXT_POSIX_DIR, its bucket/prefix env vars are set
    by the same process, so env wins as a consistent set; the Brain contract
    bucket is used only to build a POSIX path when env is absent.
    """
    env_dir = os.environ.get("KANIKO_CONTEXT_POSIX_DIR")
    if env_dir:
        bucket = os.environ.get("KANIKO_CONTEXT_S3_BUCKET", DEFAULT_BUCKET)
        prefix = os.environ.get("KANIKO_CONTEXT_S3_PREFIX", DEFAULT_PREFIX)
        return env_dir, bucket, prefix
    bucket = (
        os.environ.get("KANIKO_CONTEXT_S3_BUCKET")
        or runtime.get("bucket")
        or DEFAULT_BUCKET
    )
    prefix = os.environ.get("KANIKO_CONTEXT_S3_PREFIX", DEFAULT_PREFIX)
    root = os.environ.get("VERSITYGW_ROOT") or os.path.join(
        os.environ.get("CODEX_GATEWAY_CWD", "/home/devbox/workspace"), ".versitygw-s3"
    )
    return os.path.join(root, bucket, prefix), bucket, prefix


def resolve_deadline_seconds(runtime, flag_timeout):
    if flag_timeout:
        if flag_timeout > MAX_BUILD_SECONDS:
            fail(f"--timeout must be <= {MAX_BUILD_SECONDS}")
        return flag_timeout
    seconds = runtime.get("buildDeadlineSeconds")
    seconds = int(seconds) if isinstance(seconds, (int, float)) and seconds > 0 else MAX_BUILD_SECONDS
    deadline_at = runtime.get("buildDeadlineAt")
    if isinstance(deadline_at, str):
        try:
            deadline_epoch = calendar.timegm(
                time.strptime(deadline_at[:19], "%Y-%m-%dT%H:%M:%S")
            )
            remaining = int(deadline_epoch - time.time())
            if remaining <= 0:
                fail("build deadline from build-runtime.json has already elapsed")
            seconds = min(seconds, remaining)
        except ValueError:
            log(f"warning: unparsable buildDeadlineAt {deadline_at!r}; using {seconds}s")
    return min(seconds, MAX_BUILD_SECONDS)


# ── GHCR ─────────────────────────────────────────────────


def check_ghcr_token(target_image):
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        fail("GITHUB_TOKEN is required to push to ghcr.io")
    status, headers, user = http_json(
        "https://api.github.com/user",
        headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": "use-sealos-kaniko",
            "Accept": "application/vnd.github+json",
        },
    )
    if status != 200 or not isinstance(user, dict) or not user.get("login"):
        fail(f"GITHUB_TOKEN validation failed (GitHub /user returned {status})")
    login = user["login"]
    scopes = [s.strip() for s in (headers.get("x-oauth-scopes") or "").split(",") if s.strip()]
    if scopes and "write:packages" not in scopes:
        fail("GITHUB_TOKEN lacks the write:packages scope", scopes=scopes)
    owner = target_image.split("/")[1]
    if owner != owner.lower():
        fail(f"GHCR owner must be lowercase: {owner}")
    if owner != login.lower():
        fail(
            f"target image owner {owner!r} does not match the token login {login.lower()!r}",
            hint="use ghcr.io/<token-login>/<repo>:<tag>",
        )
    return login, token


def classify_pull(image_repo, digest):
    """Can the pushed image be pulled anonymously? GHCR issues anonymous
    tokens for public packages only."""
    path = image_repo.split("/", 1)[1]
    status, _, tok = http_json(
        f"https://ghcr.io/token?scope=repository:{urllib.parse.quote(path)}:pull&service=ghcr.io"
    )
    if status != 200 or not isinstance(tok, dict) or not tok.get("token"):
        return "private" if status in (401, 403) else "indeterminate"
    req = urllib.request.Request(
        f"https://ghcr.io/v2/{path}/manifests/{digest}",
        headers={
            "Authorization": f"Bearer {tok['token']}",
            "Accept": "application/vnd.oci.image.index.v1+json, "
            "application/vnd.docker.distribution.manifest.v2+json, "
            "application/vnd.oci.image.manifest.v1+json",
        },
        method="HEAD",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return "anonymous" if resp.status == 200 else "indeterminate"
    except urllib.error.HTTPError as e:
        return "private" if e.code in (401, 403, 404) else "indeterminate"
    except (urllib.error.URLError, TimeoutError):
        return "indeterminate"


# ── build steps ──────────────────────────────────────────


def validate_image(image):
    if not image.startswith("ghcr.io/"):
        fail(f"target image must be on ghcr.io: {image}")
    if "@" in image:
        fail("target image must be a tag reference, not a digest")
    repo, _, tag = image.partition(":")
    if not tag or "/" in tag:
        fail(f"target image must include a tag: {image}")
    if len(repo.split("/")) < 3:
        fail(f"target image must be ghcr.io/<owner>/<repo>: {image}")
    return repo, tag


def sanitize_dns_label(value, max_length):
    value = re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-") or "build"
    return value[:max_length].rstrip("-")


def prepare_context_tar(context_dir, dockerfile, posix_dir, prefix, devbox, build_id):
    context_dir = os.path.realpath(context_dir)
    if not os.path.isdir(context_dir):
        fail(f"context directory not found: {context_dir}")
    dockerfile_abs = os.path.realpath(os.path.join(context_dir, dockerfile))
    if not dockerfile_abs.startswith(context_dir + os.sep):
        fail(f"dockerfile must live inside the context: {dockerfile}")
    if not os.path.isfile(dockerfile_abs):
        fail(f"dockerfile not found: {dockerfile_abs}")
    dockerfile_rel = os.path.relpath(dockerfile_abs, context_dir).replace(os.sep, "/")

    object_dir = os.path.join(posix_dir, devbox, build_id)
    os.makedirs(object_dir, exist_ok=True)
    tar_path = os.path.join(object_dir, "context.tar.gz")
    exclude_args = []
    for name in TAR_EXCLUDES:
        exclude_args += ["--exclude", f"./{name}"]
    code, _, err = run(
        ["tar", *exclude_args, "-C", context_dir, "-czf", tar_path, "."], timeout=600
    )
    if code != 0:
        fail(f"tar failed: {err.strip()[:500]}")
    size = os.path.getsize(tar_path)
    log(f"context: {tar_path} ({size} bytes), dockerfile: {dockerfile_rel}")
    object_key = f"{prefix}/{devbox}/{build_id}/context.tar.gz"
    return object_key, dockerfile_rel, size


def create_registry_secret(namespace, login, token, secret_name):
    auth = base64.b64encode(f"{login}:{token}".encode()).decode()
    docker_config = json.dumps({"auths": {"ghcr.io": {"auth": auth}}})
    with tempfile.TemporaryDirectory() as tmp:
        config_path = os.path.join(tmp, "config.json")
        with open(config_path, "w") as f:
            f.write(docker_config)
        os.chmod(config_path, 0o600)
        code, _, err = kubectl(
            [
                "create", "secret", "generic", secret_name,
                "-n", namespace,
                f"--from-file=config.json={config_path}",
            ]
        )
    if code != 0:
        fail(f"failed to create registry secret: {err.strip()[:500]}")


def s3_credential_env(runtime, namespace):
    """Kaniko Job env entries for the S3 credentials.

    Prefer the existing DevBox secret named by the Brain runtime contract;
    fall back to creating a build-only secret from the local env.
    Returns (env_yaml_lines, secret_to_cleanup_or_None).
    """
    access_key = runtime.get("accessKeyId") or os.environ.get("AWS_ACCESS_KEY_ID", "admin")
    ref = runtime.get("secretKeyRef")
    if isinstance(ref, dict) and ref.get("name") and ref.get("key"):
        code, _, _ = kubectl(["get", "secret", ref["name"], "-n", namespace])
        if code == 0:
            lines = [
                yaml_env_literal("AWS_ACCESS_KEY_ID", access_key),
                yaml_env_secret("AWS_SECRET_ACCESS_KEY", ref["name"], ref["key"]),
            ]
            return lines, None
        log(f"warning: runtime secretKeyRef {ref['name']} not found; falling back to env")
    secret_value = (
        os.environ.get("AWS_SECRET_ACCESS_KEY")
        or os.environ.get("SEALOS_DEVBOX_JWT_SECRET")
        or os.environ.get("DEVBOX_JWT_SECRET")
    )
    if not secret_value:
        fail(
            "no S3 credentials: build-runtime.json secretKeyRef is unusable and "
            "AWS_SECRET_ACCESS_KEY / SEALOS_DEVBOX_JWT_SECRET / DEVBOX_JWT_SECRET are unset"
        )
    secret_name = f"use-sealos-kaniko-s3-{uuid.uuid4().hex[:8]}"
    with tempfile.TemporaryDirectory() as tmp:
        env_file = os.path.join(tmp, "s3.env")
        with open(env_file, "w") as f:
            f.write(f"AWS_ACCESS_KEY_ID={access_key}\nAWS_SECRET_ACCESS_KEY={secret_value}\n")
        os.chmod(env_file, 0o600)
        code, _, err = kubectl(
            ["create", "secret", "generic", secret_name, "-n", namespace,
             f"--from-env-file={env_file}"]
        )
    if code != 0:
        fail(f"failed to create S3 secret: {err.strip()[:500]}")
    lines = [
        yaml_env_secret("AWS_ACCESS_KEY_ID", secret_name, "AWS_ACCESS_KEY_ID"),
        yaml_env_secret("AWS_SECRET_ACCESS_KEY", secret_name, "AWS_SECRET_ACCESS_KEY"),
    ]
    return lines, secret_name


def yaml_quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def yaml_env_literal(name, value):
    return f"        - name: {name}\n          value: {yaml_quote(value)}"


def yaml_env_secret(name, secret, key):
    return (
        f"        - name: {name}\n"
        f"          valueFrom:\n"
        f"            secretKeyRef:\n"
        f"              name: {secret}\n"
        f"              key: {key}"
    )


def validate_build_arg(pair):
    key, sep, _ = pair.partition("=")
    if not sep or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
        fail(f"--build-arg must be KEY=value with an env-style key: {pair}")
    if "\n" in pair or "\r" in pair:
        fail(f"--build-arg value must be single-line: {key}")
    return pair


# ── resource fitting ─────────────────────────────────────

QUANTITY_RE = re.compile(
    r"^([0-9]+(?:\.[0-9]*)?|\.[0-9]+)([eE][+-]?[0-9]+)?(Ki|Mi|Gi|Ti|Pi|Ei|[mkMGTPE])?$"
)
BINARY_SUFFIXES = {"Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4, "Pi": 1024**5, "Ei": 1024**6}
DECIMAL_SUFFIXES = {"m": "0.001", "k": "1e3", "M": "1e6", "G": "1e9", "T": "1e12", "P": "1e15", "E": "1e18"}


class ResourceFitError(Exception):
    """The namespace quota cannot host even the smallest acceptable Job."""

    def __init__(self, message, quota):
        super().__init__(message)
        self.quota = quota


def parse_quantity(value, dimension):
    """Kubernetes quantity -> integer canonical unit (millicores for cpu, bytes otherwise)."""
    text = str(value).strip()
    match = QUANTITY_RE.match(text)
    if not match:
        raise ValueError(f"invalid quantity: {value!r}")
    number, exponent, suffix = match.groups()
    amount = decimal.Decimal(number + (exponent or ""))
    if suffix in BINARY_SUFFIXES:
        amount *= BINARY_SUFFIXES[suffix]
    elif suffix:
        amount *= decimal.Decimal(DECIMAL_SUFFIXES[suffix])
    if dimension == "cpu":
        amount *= 1000
    return int(amount.to_integral_value(rounding=decimal.ROUND_CEILING))


def format_quantity(amount, dimension):
    if dimension == "cpu":
        return str(amount // 1000) if amount % 1000 == 0 else f"{amount}m"
    for suffix in ("Gi", "Mi", "Ki"):
        unit = BINARY_SUFFIXES[suffix]
        if amount and amount % unit == 0:
            return f"{amount // unit}{suffix}"
    return str(amount)


def quota_remaining(quotas):
    """Per-dimension remaining quota from ResourceQuota objects.

    Returns {"limits.<dim>" | "requests.<dim>": {"hard", "used", "remaining"}}
    with integer canonical units. The bare `memory`/`cpu`/`ephemeral-storage`
    keys are aliases for `requests.*`. When several quotas constrain the same
    key the tightest remaining wins. Scoped quotas are treated as applying,
    which can only make the fit more conservative.
    """
    remaining = {}
    for quota in quotas:
        status = quota.get("status") or {}
        hard = status.get("hard") or (quota.get("spec") or {}).get("hard") or {}
        used = status.get("used") or {}
        for raw_key, hard_value in hard.items():
            key = raw_key if "." in raw_key else f"requests.{raw_key}"
            kind, _, dimension = key.partition(".")
            if kind not in ("limits", "requests") or dimension not in RESOURCE_DIMENSIONS:
                continue
            try:
                hard_amount = parse_quantity(hard_value, dimension)
                used_amount = parse_quantity(used.get(raw_key, "0"), dimension)
            except ValueError as e:
                log(f"warning: ignoring quota {quota.get('metadata', {}).get('name')}: {e}")
                continue
            entry = {
                "hard": hard_amount,
                "used": used_amount,
                "remaining": max(hard_amount - used_amount, 0),
                "quota": (quota.get("metadata") or {}).get("name"),
            }
            if key not in remaining or entry["remaining"] < remaining[key]["remaining"]:
                remaining[key] = entry
    return remaining


def describe_quota_entry(entry, dimension):
    return {
        "quota": entry["quota"],
        "hard": format_quantity(entry["hard"], dimension),
        "used": format_quantity(entry["used"], dimension),
        "remaining": format_quantity(entry["remaining"], dimension),
    }


def fit_resources(remaining, overrides=None):
    """Size the Job's requests/limits to the remaining quota.

    Per dimension: limit = min(ceiling, remaining limits.<dim>) where the
    ceiling is the default limit, or the --*-limit override which is then
    also the floor (explicit values are honored verbatim or rejected).
    request = min(default request, limit, remaining requests.<dim>), floored
    at the request floor. Raises ResourceFitError when a floor does not fit.
    Returns {"requests": {dim: str}, "limits": {dim: str}, "adjusted": [..]}.
    """
    overrides = overrides or {}
    resources = {"requests": {}, "limits": {}, "adjusted": []}
    for dimension in RESOURCE_DIMENSIONS:
        defaults = RESOURCE_DEFAULTS[dimension]
        override = overrides.get(dimension)
        if override is not None:
            limit_ceiling = limit_floor = parse_quantity(override, dimension)
        else:
            limit_ceiling = parse_quantity(defaults["limit"], dimension)
            limit_floor = parse_quantity(defaults["limit_floor"], dimension)
        limit = limit_ceiling
        limit_quota = remaining.get(f"limits.{dimension}")
        if limit_quota is not None and limit_quota["remaining"] < limit:
            limit = limit_quota["remaining"]
            resources["adjusted"].append(f"limits.{dimension}")
        if limit < limit_floor:
            source = "requested" if override is not None else "floor"
            raise ResourceFitError(
                f"namespace quota cannot fit the kaniko job: limits.{dimension} remaining "
                f"{format_quantity(limit, dimension)} < {source} "
                f"{format_quantity(limit_floor, dimension)}",
                {f"limits.{dimension}": describe_quota_entry(limit_quota, dimension)},
            )

        request = min(parse_quantity(defaults["request"], dimension), limit)
        request_floor = min(parse_quantity(defaults["request_floor"], dimension), limit)
        request_quota = remaining.get(f"requests.{dimension}")
        if request_quota is not None and request_quota["remaining"] < request:
            request = request_quota["remaining"]
            resources["adjusted"].append(f"requests.{dimension}")
        if request < request_floor:
            raise ResourceFitError(
                f"namespace quota cannot fit the kaniko job: requests.{dimension} remaining "
                f"{format_quantity(request, dimension)} < floor "
                f"{format_quantity(request_floor, dimension)}",
                {f"requests.{dimension}": describe_quota_entry(request_quota, dimension)},
            )
        resources["limits"][dimension] = format_quantity(limit, dimension)
        resources["requests"][dimension] = format_quantity(request, dimension)
    return resources


def read_resource_quotas(namespace):
    code, out, err = kubectl(["get", "resourcequota", "-n", namespace, "-o", "json"])
    if code != 0:
        log(f"warning: cannot read ResourceQuota in {namespace}; using defaults: {err.strip()[:200]}")
        return []
    try:
        items = json.loads(out or "{}").get("items") or []
    except ValueError:
        log("warning: ResourceQuota output is not JSON; using defaults")
        return []
    return [item for item in items if isinstance(item, dict)]


def resolve_resources(namespace, overrides, consult_quota=True):
    quotas = read_resource_quotas(namespace) if consult_quota else []
    remaining = quota_remaining(quotas)
    try:
        resources = fit_resources(remaining, overrides)
    except ResourceFitError as e:
        fail(
            str(e),
            namespace=namespace,
            quota=e.quota,
            hint="free namespace quota or pass a smaller --memory-limit/--cpu-limit/--ephemeral-limit",
        )
    quota_note = (
        f"quota {sorted({entry['quota'] for entry in remaining.values()})}"
        if remaining
        else "no ResourceQuota constraint" if consult_quota else "quota not consulted"
    )
    adjusted = f"; shrunk to fit: {', '.join(resources['adjusted'])}" if resources["adjusted"] else ""
    log(
        f"resources: requests {resources['requests']} limits {resources['limits']} "
        f"({quota_note}{adjusted})"
    )
    return resources


def render_resources(resources):
    lines = []
    for section in ("requests", "limits"):
        lines.append(f"          {section}:")
        for dimension in RESOURCE_DIMENSIONS:
            lines.append(f"            {dimension}: {json.dumps(resources[section][dimension])}")
    return "\n".join(lines)


def render_job(
    *, job_name, namespace, service_account, kaniko_image, platform, context_uri,
    dockerfile, target_image, s3_endpoint, aws_region, registry_secret,
    s3_env_lines, build_args, deadline_seconds, resources,
):
    for name, value in [("namespace", namespace), ("job name", job_name)]:
        if not DNS_LABEL_RE.match(value) or len(value) > 63:
            fail(f"{name} must be a DNS label up to 63 chars: {value}")
    build_arg_lines = "".join(
        f"        - {yaml_quote(f'--build-arg={pair}')}\n" for pair in build_args
    )
    sa_line = f"      serviceAccountName: {service_account}\n" if service_account else ""
    return f"""apiVersion: batch/v1
kind: Job
metadata:
  name: {job_name}
  namespace: {namespace}
  labels:
    app.kubernetes.io/name: use-sealos-kaniko
    app.kubernetes.io/managed-by: use-sealos
  annotations:
    use-sealos.dev/context-uri: {yaml_quote(context_uri)}
    use-sealos.dev/target-image: {yaml_quote(target_image)}
spec:
  activeDeadlineSeconds: {deadline_seconds}
  backoffLimit: 0
  ttlSecondsAfterFinished: 3600
  template:
    metadata:
      labels:
        app.kubernetes.io/name: use-sealos-kaniko
    spec:
      restartPolicy: Never
{sa_line}      containers:
      - name: kaniko
        image: {kaniko_image}
        resources:
{render_resources(resources)}
        args:
        - {yaml_quote(f'--dockerfile={dockerfile}')}
        - {yaml_quote(f'--context={context_uri}')}
        - {yaml_quote(f'--destination={target_image}')}
        - {yaml_quote(f'--custom-platform={platform}')}
        - '--digest-file=/dev/termination-log'
        - '--cleanup'
        - '--verbosity=info'
{build_arg_lines}        env:
        - name: S3_ENDPOINT
          value: {yaml_quote(s3_endpoint)}
        - name: S3_FORCE_PATH_STYLE
          value: "true"
        - name: AWS_EC2_METADATA_DISABLED
          value: "true"
        - name: AWS_REGION
          value: {yaml_quote(aws_region)}
{chr(10).join(s3_env_lines)}
        volumeMounts:
        - name: docker-config
          mountPath: /kaniko/.docker/config.json
          subPath: config.json
          readOnly: true
      volumes:
      - name: docker-config
        secret:
          secretName: {registry_secret}
          items:
          - key: config.json
            path: config.json
"""


def collect_failure_diagnostics(namespace, job_name):
    diagnostics = []
    for label, cmd in [
        ("job", ["get", "job", job_name, "-n", namespace, "-o", "jsonpath={.status}"]),
        ("pods", ["get", "pods", "-n", namespace, "-l", f"job-name={job_name}", "-o", "wide"]),
        ("events", ["get", "events", "-n", namespace, "--field-selector",
                    f"involvedObject.name={job_name}", "-o",
                    "custom-columns=LAST:.lastTimestamp,REASON:.reason,MESSAGE:.message"]),
        ("logs", ["logs", f"job/{job_name}", "-n", namespace, "--tail=60"]),
    ]:
        _, out, err = kubectl(cmd)
        text = (out or err).strip()
        if text:
            diagnostics.append(f"--- {label} ---\n{text[:3000]}")
    return "\n".join(diagnostics)


# ── job watching ─────────────────────────────────────────


def parse_k8s_timestamp(value):
    try:
        return calendar.timegm(time.strptime(str(value)[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return None


def job_condition(job, condition_type):
    for condition in ((job.get("status") or {}).get("conditions") or []):
        if condition.get("type") == condition_type and condition.get("status") == "True":
            return condition
    return None


def classify_job_state(job, pods, events, now):
    """Decide whether to keep waiting. Pure: inputs are kubectl JSON objects.

    Returns (state, reason, detail) with state in {"complete", "failed",
    "waiting"}. `reason` is a stable code for the failure JSON; `detail` is
    the human-readable message pulled from the cluster.
    """
    if job_condition(job, "Complete"):
        return "complete", None, None
    failed = job_condition(job, "Failed")
    if failed:
        return "failed", "job_failed", f"{failed.get('reason', 'Failed')}: {failed.get('message', '')}".strip(": ")

    for pod in pods:
        name = (pod.get("metadata") or {}).get("name", "?")
        status = pod.get("status") or {}
        phase = status.get("phase")
        if phase == "Failed":
            detail = status.get("message") or status.get("reason") or ""
            for cs in status.get("containerStatuses") or []:
                terminated = (cs.get("state") or {}).get("terminated") or {}
                if terminated:
                    detail = (
                        f"container {cs.get('name')} exited {terminated.get('exitCode')} "
                        f"({terminated.get('reason', '')})"
                    ).strip()
            return "failed", "pod_failed", f"pod {name} failed: {detail}".rstrip(": ")
        for cs in (status.get("containerStatuses") or []) + (status.get("initContainerStatuses") or []):
            waiting = (cs.get("state") or {}).get("waiting") or {}
            if waiting.get("reason") in FATAL_WAITING_REASONS:
                return (
                    "failed",
                    "image_pull" if "Image" in waiting["reason"] else "container_create",
                    f"pod {name} container {cs.get('name')} {waiting['reason']}: "
                    f"{waiting.get('message', '')}".rstrip(": "),
                )
        if phase == "Pending":
            for condition in status.get("conditions") or []:
                if (
                    condition.get("type") == "PodScheduled"
                    and condition.get("status") == "False"
                    and condition.get("reason") == "Unschedulable"
                ):
                    created = parse_k8s_timestamp((pod.get("metadata") or {}).get("creationTimestamp"))
                    age = now - created if created is not None else 0
                    if age >= PENDING_GRACE_SECONDS:
                        return (
                            "failed",
                            "unschedulable",
                            f"pod {name} unschedulable for {int(age)}s: {condition.get('message', '')}".rstrip(": "),
                        )

    if not pods:
        for event in events:
            if event.get("reason") == "FailedCreate":
                return "failed", "failed_create", f"FailedCreate: {event.get('message', '')}".rstrip(": ")
    return "waiting", None, None


def kubectl_json(args):
    code, out, err = kubectl(args)
    if code != 0:
        log(f"warning: kubectl {' '.join(args[:3])} failed: {err.strip()[:200]}")
        return None
    try:
        return json.loads(out or "{}")
    except ValueError:
        return None


def wait_for_job(namespace, job_name, wait_timeout, poll_interval=POLL_INTERVAL_SECONDS):
    """Poll until the Job completes; return (reason, detail) on failure, None on success."""
    started = time.monotonic()
    last_state = None
    while True:
        job = kubectl_json(["get", "job", job_name, "-n", namespace, "-o", "json"])
        if job is None:
            job = {}
        pods = (kubectl_json(["get", "pods", "-n", namespace, "-l", f"job-name={job_name}", "-o", "json"]) or {}).get("items") or []
        events = []
        if not pods:
            events = (
                kubectl_json([
                    "get", "events", "-n", namespace, "--field-selector",
                    f"involvedObject.name={job_name},involvedObject.kind=Job", "-o", "json",
                ])
                or {}
            ).get("items") or []
        state, reason, detail = classify_job_state(job, pods, events, time.time())
        if state == "complete":
            return None
        if state == "failed":
            return reason, detail
        elapsed = time.monotonic() - started
        snapshot = ", ".join(
            f"{(p.get('metadata') or {}).get('name')}={(p.get('status') or {}).get('phase')}" for p in pods
        ) or "no pod yet"
        if snapshot != last_state:
            log(f"job {job_name}: {snapshot} ({int(elapsed)}s)")
            last_state = snapshot
        if elapsed >= wait_timeout:
            return "timeout", f"job did not complete within {wait_timeout}s"
        time.sleep(min(poll_interval, max(wait_timeout - elapsed, 0.1)))


def read_digest(namespace, job_name):
    code, out, err = kubectl(
        [
            "get", "pods", "-n", namespace, "-l", f"job-name={job_name}",
            "-o",
            'jsonpath={.items[*].status.containerStatuses[?(@.name=="kaniko")].state.terminated.message}',
        ]
    )
    if code != 0:
        log(f"warning: could not read termination message: {err.strip()[:200]}")
        return None
    match = DIGEST_RE.search(out or "")
    return match.group(0) if match else None


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--image", required=True, help="target ghcr.io/<owner>/<repo>:<tag>")
    parser.add_argument("--context", default=".", help="build context directory (default: .)")
    parser.add_argument("--dockerfile", default="Dockerfile", help="path relative to the context")
    parser.add_argument("--namespace", help="target namespace (default: resolved)")
    parser.add_argument("--build-arg", action="append", default=[], metavar="K=V",
                        help="kaniko --build-arg; values appear in the Job spec, never pass secrets")
    parser.add_argument("--runtime-file", help="path to .sealos/build-runtime.json")
    parser.add_argument("--platform", default=DEFAULT_PLATFORM)
    parser.add_argument("--kaniko-image", default=DEFAULT_KANIKO_IMAGE)
    parser.add_argument("--timeout", type=int, help=f"build seconds cap (max {MAX_BUILD_SECONDS})")
    parser.add_argument("--render-only", action="store_true",
                        help="print the Job manifest and exit; no kubectl, no tar upload")
    parser.add_argument("--memory-limit", metavar="Q",
                        help=f"exact memory limit (default: up to {RESOURCE_DEFAULTS['memory']['limit']}, fitted to quota)")
    parser.add_argument("--cpu-limit", metavar="Q",
                        help=f"exact cpu limit (default: up to {RESOURCE_DEFAULTS['cpu']['limit']}, fitted to quota)")
    parser.add_argument("--ephemeral-limit", metavar="Q",
                        help=f"exact ephemeral-storage limit (default: up to {RESOURCE_DEFAULTS['ephemeral-storage']['limit']}, fitted to quota)")
    args = parser.parse_args()

    image_repo, image_tag = validate_image(args.image)
    for pair in args.build_arg:
        validate_build_arg(pair)
    overrides = {
        dimension: value
        for dimension, value in [
            ("cpu", args.cpu_limit),
            ("memory", args.memory_limit),
            ("ephemeral-storage", args.ephemeral_limit),
        ]
        if value
    }
    for dimension, value in overrides.items():
        try:
            parse_quantity(value, dimension)
        except ValueError:
            fail(f"invalid --{dimension.split('-')[0]}-limit quantity: {value}")

    workspace = os.environ.get("SEALAI_DEPLOY_WORKSPACE") or os.getcwd()
    runtime_path = args.runtime_file or os.path.join(workspace, ".sealos", "build-runtime.json")
    runtime = load_runtime_contract(runtime_path)
    deadline_seconds = resolve_deadline_seconds(runtime, args.timeout)
    aws_region = runtime.get("region") or os.environ.get("AWS_REGION", "sealos-internal")
    devbox = (
        runtime.get("devboxName")
        or os.environ.get("SEALOS_DEVBOX_NAME")
        or os.environ.get("DEVBOX_NAME")
        or os.environ.get("HOSTNAME")
        or "devbox"
    )
    build_id = f"{sanitize_dns_label(image_repo.rsplit('/', 1)[-1], 30)}-{uuid.uuid4().hex[:8]}"
    job_name = f"kaniko-{build_id}"[:63].rstrip("-")

    if args.render_only:
        posix_dir, bucket, prefix = resolve_context_store(runtime)
        render_namespace = args.namespace or os.environ.get("SEALAI_NAMESPACE", "ns-example")
        manifest = render_job(
            job_name=job_name,
            namespace=render_namespace,
            service_account=os.environ.get("SERVICE_ACCOUNT_NAME"),
            kaniko_image=args.kaniko_image,
            platform=args.platform,
            context_uri=f"s3://{bucket}/{prefix}/{devbox}/{build_id}/context.tar.gz",
            dockerfile=args.dockerfile,
            target_image=args.image,
            s3_endpoint=runtime.get("s3Endpoint")
            or os.environ.get("KANIKO_JOB_S3_ENDPOINT", "http://devbox-net:1319"),
            aws_region=aws_region,
            registry_secret="use-sealos-ghcr-auth-render",
            s3_env_lines=(
                [
                    yaml_env_literal(
                        "AWS_ACCESS_KEY_ID", runtime.get("accessKeyId") or "admin"
                    ),
                    yaml_env_secret(
                        "AWS_SECRET_ACCESS_KEY",
                        (runtime.get("secretKeyRef") or {}).get("name", "devbox-secret"),
                        (runtime.get("secretKeyRef") or {}).get("key", "SEALOS_DEVBOX_JWT_SECRET"),
                    ),
                ]
            ),
            build_args=args.build_arg,
            deadline_seconds=deadline_seconds,
            resources=resolve_resources(render_namespace, overrides, consult_quota=False),
        )
        print(manifest)
        return

    if shutil.which("kubectl") is None:
        fail("kubectl is required")
    if shutil.which("tar") is None:
        fail("tar is required")

    namespace, ns_source = resolve_namespace(args.namespace)
    log(f"namespace: {namespace} (from {ns_source})")
    service_account = resolve_service_account(namespace)
    if service_account:
        log(f"service account: {service_account}")

    login, token = check_ghcr_token(args.image)
    log(f"ghcr: authenticated as {login}")

    resources = resolve_resources(namespace, overrides)

    s3_endpoint = resolve_job_s3_endpoint(runtime, namespace)
    posix_dir, bucket, prefix = resolve_context_store(runtime)
    log(f"s3: job endpoint {s3_endpoint}, bucket {bucket}, posix dir {posix_dir}")

    object_key, dockerfile_rel, tar_size = prepare_context_tar(
        args.context, args.dockerfile, posix_dir, prefix, devbox, build_id
    )
    context_uri = f"s3://{bucket}/{object_key}"

    registry_secret = f"use-sealos-ghcr-auth-{uuid.uuid4().hex[:8]}"
    create_registry_secret(namespace, login, token, registry_secret)
    s3_env_lines, s3_secret = s3_credential_env(runtime, namespace)

    manifest = render_job(
        job_name=job_name,
        namespace=namespace,
        service_account=service_account,
        kaniko_image=args.kaniko_image,
        platform=args.platform,
        context_uri=context_uri,
        dockerfile=dockerfile_rel,
        target_image=args.image,
        s3_endpoint=s3_endpoint,
        aws_region=aws_region,
        registry_secret=registry_secret,
        s3_env_lines=s3_env_lines,
        build_args=args.build_arg,
        deadline_seconds=deadline_seconds,
        resources=resources,
    )
    code, _, err = kubectl(["apply", "-f", "-"], input_text=manifest)
    if code != 0:
        fail(f"kubectl apply failed: {err.strip()[:500]}")
    wait_timeout = deadline_seconds + WAIT_SLACK_SECONDS
    log(f"job {job_name} created; polling every {POLL_INTERVAL_SECONDS}s, up to {wait_timeout}s")

    failure = wait_for_job(namespace, job_name, wait_timeout)
    if failure:
        reason, detail = failure
        diagnostics = collect_failure_diagnostics(namespace, job_name)
        log(diagnostics)
        fail(
            f"kaniko job did not complete ({reason}): {detail}",
            job=job_name,
            namespace=namespace,
            reason=reason,
            detail=detail[:500],
            resources={"requests": resources["requests"], "limits": resources["limits"]},
            diagnostics_tail=diagnostics[-1500:],
        )

    digest = read_digest(namespace, job_name)
    pull = classify_pull(image_repo, digest) if digest else "indeterminate"
    result = {
        "success": True,
        "image": args.image,
        "digest": digest,
        "image_ref": f"{image_repo}@{digest}" if digest else args.image,
        "pull": pull,
        "job": job_name,
        "namespace": namespace,
        "context_uri": context_uri,
        "context_bytes": tar_size,
        "registry_secret": registry_secret,
        **({"s3_secret": s3_secret} if s3_secret else {}),
    }
    if not digest:
        result["warning"] = (
            "image pushed but digest could not be read from the pod termination "
            "message; deploy with the tag reference"
        )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
