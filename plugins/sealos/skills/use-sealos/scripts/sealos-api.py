#!/usr/bin/env python3
"""Sealos Cloud API helper: auth, template deploys, store queries.

Standard library only. Credentials live in ~/.sealos/:
  kubeconfig   kubectl-compatible credentials for the current workspace
  auth.json    region + tokens (written by `login`, needed only for login/switch)

The kubeconfig alone is enough for every non-login command, so users can also
paste a kubeconfig from the Sealos web console instead of running `login`.

Commands:
  status                         auth + region + namespace summary
  login [--region URL]           OAuth2 device-flow login, saves kubeconfig
  workspaces                     list workspaces of the signed-in account
  switch <workspace>             switch workspace, refresh kubeconfig
  deploy <template.yaml> [...]   deploy a local template YAML
  deploy-store <template> [...]  deploy a template-store template by name
  adopt <instance> [--template-name NAME]
                                 claim an existing instance as a Brain Project
                                 (no-op outside *.sealos.io regions)
  store-list [--search Q]        list template-store templates
  store-get <template> [--yaml]  template inputs/quota (and source YAML)
  store-export <template> --out F  write a store template's source to a file
  instances                      list deployed template instances
  delete <instance>              delete an instance and all its resources
"""

import argparse
import base64
import hashlib
import ipaddress
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

CLIENT_ID = "af993c98-d19d-4bdc-b338-79b80dc4f8bf"
DEFAULT_REGION = "https://usw-1.sealos.io"
KNOWN_REGIONS = [
    "https://usw-1.sealos.io",
    "https://gzg.sealos.run",
    "https://bja.sealos.run",
    "https://hzh.sealos.run",
]

SEALOS_DIR = os.path.expanduser("~/.sealos")
AUTH_PATH = os.path.join(SEALOS_DIR, "auth.json")
DEFAULT_KUBECONFIG_PATH = os.path.join(SEALOS_DIR, "kubeconfig")


def resolve_kubeconfig_path():
    """Explicit SEALOS_KUBECONFIG wins; then the login-owned default; then an
    ambient KUBECONFIG (e.g. injected into a managed sandbox) if it exists."""
    explicit = os.environ.get("SEALOS_KUBECONFIG")
    if explicit:
        return explicit
    if os.path.exists(DEFAULT_KUBECONFIG_PATH):
        return DEFAULT_KUBECONFIG_PATH
    ambient = os.environ.get("KUBECONFIG")
    if ambient and os.path.exists(ambient):
        return ambient
    return DEFAULT_KUBECONFIG_PATH


KUBECONFIG_PATH = resolve_kubeconfig_path()
# `login`/`switch` never write to an ambient KUBECONFIG.
KUBECONFIG_WRITE_PATH = os.environ.get("SEALOS_KUBECONFIG") or DEFAULT_KUBECONFIG_PATH


def fail(message, **extra):
    print(json.dumps({"error": message, **extra}), file=sys.stderr)
    sys.exit(1)


def http_json(url, method="GET", headers=None, data=None, form=None, timeout=30, fatal=True):
    """Make an HTTP request; return (status, parsed-json-or-text)."""
    body = None
    headers = dict(headers or {})
    if form is not None:
        body = urllib.parse.urlencode(form).encode()
        headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
    elif data is not None:
        body = json.dumps(data).encode()
        headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode()
            status = resp.status
    except urllib.error.HTTPError as e:
        text = e.read().decode(errors="replace")
        status = e.code
    except (urllib.error.URLError, TimeoutError) as e:
        if not fatal:
            return 0, {"error": f"request to {url} failed: {e}"}
        fail(f"request to {url} failed: {e}")
    try:
        return status, json.loads(text)
    except ValueError:
        return status, text


def load_auth():
    if not os.path.exists(AUTH_PATH):
        return {}
    try:
        with open(AUTH_PATH) as f:
            return json.load(f)
    except ValueError:
        return {}


def load_kubeconfig():
    if not os.path.exists(KUBECONFIG_PATH):
        fail(
            "kubeconfig not found; run `sealos-api.py login` or save one from the Sealos web console",
            path=KUBECONFIG_PATH,
        )
    with open(KUBECONFIG_PATH) as f:
        return f.read()


def kubeconfig_field(kubeconfig, field):
    m = re.search(rf"^\s*{field}:\s*[\"']?([^\"'\s]+)", kubeconfig, re.MULTILINE)
    return m.group(1) if m else None


KUBECONFIG_FILE_REF_RE = re.compile(
    r"^(\s*(?:-\s+)?)(certificate-authority|client-certificate|client-key|tokenFile)(?!-data):\s*(.+?)\s*$"
)


def kubeconfig_has_file_refs(kubeconfig):
    return any(
        KUBECONFIG_FILE_REF_RE.match(line) for line in (kubeconfig or "").splitlines()
    )


def portable_kubeconfig(kubeconfig, base_dir):
    """Inline file-referenced credentials so the kubeconfig text is self-contained.

    In-cluster kubeconfigs (e.g. a Devbox sandbox) point at ca.crt / token files
    that only exist on this machine; the Template API receives the kubeconfig as
    a header and would try to open those paths on its own filesystem."""
    if not kubeconfig_has_file_refs(kubeconfig):
        return kubeconfig
    out = []
    for line in kubeconfig.splitlines(keepends=True):
        m = KUBECONFIG_FILE_REF_RE.match(line.rstrip("\r\n"))
        if not m:
            out.append(line)
            continue
        prefix, key, value = m.groups()
        path = value.strip()
        if len(path) >= 2 and path[0] == path[-1] and path[0] in "\"'":
            path = path[1:-1]
        if not os.path.isabs(path):
            path = os.path.join(base_dir, path)
        try:
            with open(path, "rb") as f:
                content = f.read()
        except OSError:
            fail("kubeconfig references a credential file that cannot be read", path=path, key=key)
        if key == "tokenFile":
            new_key, new_value = "token", content.decode(errors="replace").strip()
        else:
            new_key, new_value = key + "-data", base64.b64encode(content).decode()
        newline = line[len(line.rstrip("\r\n")) :]
        out.append(f"{prefix}{new_key}: {new_value}{newline}")
    return "".join(out)


def api_kubeconfig():
    """Kubeconfig text suitable for sending to a remote API."""
    return portable_kubeconfig(
        load_kubeconfig(), os.path.dirname(os.path.abspath(KUBECONFIG_PATH))
    )


def api_credential():
    return urllib.parse.quote(api_kubeconfig(), safe="")


def is_in_cluster_host(host):
    """True for hosts that only resolve inside a Kubernetes cluster."""
    h = (host or "").strip().lower().rstrip(".")
    if not h:
        return False
    if h in ("kubernetes", "kubernetes.default", "kubernetes.default.svc", "localhost"):
        return True
    if h.endswith(".svc") or h.endswith(".svc.cluster.local"):
        return True
    try:
        ipaddress.ip_address(h.strip("[]"))
        return True
    except ValueError:
        return False


def _host_from_region_value(value):
    value = (value or "").strip()
    if not value:
        return None
    if "://" not in value:
        value = "https://" + value
    host = urllib.parse.urlparse(value).hostname
    return host.lower() if host else None


def resolve_region_domain():
    """Region domain (e.g. usw-1.sealos.io) or None when it cannot be determined.

    SEALOS_REGION (URL or bare host) > auth.json region > kubeconfig server,
    except that an in-cluster server says nothing about the public region."""
    env_region = _host_from_region_value(os.environ.get("SEALOS_REGION"))
    if env_region:
        return env_region
    auth = load_auth()
    if auth.get("region"):
        return urllib.parse.urlparse(auth["region"]).hostname
    if not os.path.exists(KUBECONFIG_PATH):
        return None
    server = kubeconfig_field(load_kubeconfig(), "server")
    host = urllib.parse.urlparse(server).hostname if server else None
    if not host or is_in_cluster_host(host):
        return None
    return host


def _kubeconfig_server_or_none():
    if not os.path.exists(KUBECONFIG_PATH):
        return None
    return kubeconfig_field(load_kubeconfig(), "server")


def region_domain():
    """Like resolve_region_domain(), but exits with guidance when unresolvable."""
    domain = resolve_region_domain()
    if domain:
        return domain
    extra = {}
    server = _kubeconfig_server_or_none()
    if server and is_in_cluster_host(urllib.parse.urlparse(server).hostname):
        extra["server"] = server
    fail(
        "cannot determine the Sealos region: set SEALOS_REGION to the region URL "
        f"(e.g. SEALOS_REGION={DEFAULT_REGION})",
        **extra,
    )


def template_api_base_or_none():
    """Template API origin; SEALAI_TEMPLATE_API_URL overrides the region-derived host."""
    override = (os.environ.get("SEALAI_TEMPLATE_API_URL") or "").strip()
    if override:
        return override.rstrip("/")
    domain = resolve_region_domain()
    return f"https://template.{domain}" if domain else None


def template_api_base():
    return template_api_base_or_none() or f"https://template.{region_domain()}"


def save_credentials(region, access_token, regional_token, kubeconfig, workspace):
    os.makedirs(SEALOS_DIR, exist_ok=True)
    with open(KUBECONFIG_WRITE_PATH, "w") as f:
        f.write(kubeconfig)
    os.chmod(KUBECONFIG_WRITE_PATH, 0o600)
    auth = {
        "region": region,
        "access_token": access_token,
        "regional_token": regional_token,
        "authenticated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "auth_method": "oauth2_device_grant",
    }
    if workspace:
        auth["current_workspace"] = workspace
    with open(AUTH_PATH, "w") as f:
        json.dump(auth, f, indent=2)
    os.chmod(AUTH_PATH, 0o600)


# ── commands ─────────────────────────────────────────────


def cmd_status(_args):
    out = {"authenticated": False}
    if os.path.exists(KUBECONFIG_PATH):
        kc = load_kubeconfig()
        namespace = kubeconfig_field(kc, "namespace")
        server = kubeconfig_field(kc, "server")
        if server and ("token:" in kc or "tokenFile:" in kc or "client-certificate" in kc):
            auth = load_auth()
            out = {
                "authenticated": True,
                "kubeconfig": KUBECONFIG_PATH,
                "server": server,
                "namespace": namespace,
                "region_domain": resolve_region_domain(),
                "template_api": template_api_base_or_none(),
                "credential_files_inlined": kubeconfig_has_file_refs(kc),
                "workspace": (auth.get("current_workspace") or {}).get("id"),
                "authenticated_at": auth.get("authenticated_at"),
            }
    print(json.dumps(out, indent=2))


def cmd_login(args):
    region = (args.region or os.environ.get("SEALOS_REGION") or DEFAULT_REGION).rstrip("/")

    status, device = http_json(
        f"{region}/api/auth/oauth2/device",
        method="POST",
        form={
            "client_id": CLIENT_ID,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        },
    )
    if status != 200 or not isinstance(device, dict):
        fail(f"device authorization request failed ({status})", response=str(device)[:500])

    url = device.get("verification_uri_complete") or device.get("verification_uri")
    expires_in = int(device.get("expires_in", 600))
    interval = int(device.get("interval", 5))

    # Print the sign-in link immediately: the agent must relay it to the user
    # before this process finishes polling.
    print(
        f"\nOpen this URL in your browser to authorize (expires in {expires_in // 60} min):"
        f"\n\n  {url}\n\nUser code: {device.get('user_code')}\n\nWaiting for authorization...",
        file=sys.stderr,
        flush=True,
    )
    try:
        import webbrowser

        webbrowser.open(url)
    except Exception:
        pass

    deadline = time.time() + min(expires_in, 600)
    token = None
    while time.time() < deadline:
        time.sleep(interval)
        status, resp = http_json(
            f"{region}/api/auth/oauth2/token",
            method="POST",
            form={
                "client_id": CLIENT_ID,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": device.get("device_code"),
            },
        )
        if status == 200 and isinstance(resp, dict) and resp.get("access_token"):
            token = resp["access_token"]
            break
        err = resp.get("error") if isinstance(resp, dict) else None
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            interval += 5
            continue
        if err == "access_denied":
            fail("authorization denied by user")
        if err == "expired_token":
            fail("device code expired; run login again")
        fail(f"token request failed ({status})", response=str(resp)[:500])
    if not token:
        fail("authorization timed out; run login again")

    status, region_data = http_json(
        f"{region}/api/auth/regionToken", method="POST", headers={"Authorization": token}
    )
    data = region_data.get("data") if isinstance(region_data, dict) else None
    if status != 200 or not data or not data.get("token") or not data.get("kubeconfig"):
        fail(f"region token exchange failed ({status})", response=str(region_data)[:500])

    workspace = None
    status, ns_data = http_json(
        f"{region}/api/auth/namespace/list", headers={"Authorization": data["token"]}
    )
    if status == 200 and isinstance(ns_data, dict):
        namespaces = ns_data.get("data") or []
        if isinstance(namespaces, dict):
            namespaces = namespaces.get("namespaces") or []
        private = [ns for ns in namespaces if ns.get("nstype") == "private"]
        chosen = (private or namespaces or [None])[0]
        if chosen:
            workspace = {
                "uid": chosen.get("uid"),
                "id": chosen.get("id"),
                "teamName": chosen.get("teamName"),
            }

    save_credentials(region, token, data["token"], data["kubeconfig"], workspace)
    print(
        json.dumps(
            {
                "authenticated": True,
                "region": region,
                "workspace": (workspace or {}).get("id"),
                "kubeconfig": KUBECONFIG_WRITE_PATH,
            },
            indent=2,
        )
    )


def regional_token_or_fail():
    auth = load_auth()
    if not auth.get("regional_token"):
        fail("no regional token; run `sealos-api.py login` first")
    return auth


def cmd_workspaces(_args):
    auth = regional_token_or_fail()
    status, ns_data = http_json(
        f"{auth['region']}/api/auth/namespace/list",
        headers={"Authorization": auth["regional_token"]},
    )
    if status != 200:
        fail(f"list workspaces failed ({status})", response=str(ns_data)[:500])
    namespaces = ns_data.get("data") or []
    if isinstance(namespaces, dict):
        namespaces = namespaces.get("namespaces") or []
    current = (auth.get("current_workspace") or {}).get("id")
    print(
        json.dumps(
            {
                "current": current,
                "workspaces": [
                    {
                        "uid": ns.get("uid"),
                        "id": ns.get("id"),
                        "teamName": ns.get("teamName"),
                        "role": ns.get("role"),
                        "nstype": ns.get("nstype"),
                    }
                    for ns in namespaces
                ],
            },
            indent=2,
        )
    )


def cmd_switch(args):
    auth = regional_token_or_fail()
    status, ns_data = http_json(
        f"{auth['region']}/api/auth/namespace/list",
        headers={"Authorization": auth["regional_token"]},
    )
    if status != 200:
        fail(f"list workspaces failed ({status})", response=str(ns_data)[:500])
    namespaces = ns_data.get("data") or []
    if isinstance(namespaces, dict):
        namespaces = namespaces.get("namespaces") or []
    target = args.workspace.lower()
    match = next(
        (
            ns
            for ns in namespaces
            if target in (ns.get("id", "").lower(), ns.get("uid", "").lower())
            or target in ns.get("teamName", "").lower()
        ),
        None,
    )
    if not match:
        fail(
            f"no workspace matching '{args.workspace}'",
            available=[ns.get("id") for ns in namespaces],
        )

    status, switch_data = http_json(
        f"{auth['region']}/api/auth/namespace/switch",
        method="POST",
        headers={"Authorization": auth["regional_token"]},
        data={"ns_uid": match.get("uid")},
    )
    new_token = (switch_data.get("data") or {}).get("token") if isinstance(switch_data, dict) else None
    if status != 200 or not new_token:
        fail(f"switch workspace failed ({status})", response=str(switch_data)[:500])

    status, kc_data = http_json(
        f"{auth['region']}/api/auth/getKubeconfig", headers={"Authorization": new_token}
    )
    kubeconfig = (kc_data.get("data") or {}).get("kubeconfig") if isinstance(kc_data, dict) else None
    if status != 200 or not kubeconfig:
        fail(f"get kubeconfig failed ({status})", response=str(kc_data)[:500])

    workspace = {
        "uid": match.get("uid"),
        "id": match.get("id"),
        "teamName": match.get("teamName"),
    }
    save_credentials(auth["region"], auth.get("access_token"), new_token, kubeconfig, workspace)
    print(json.dumps({"switched": True, "workspace": workspace}, indent=2))


# 0 = transport failure from http_json(..., fatal=False)
ADOPT_RETRY_STATUSES = frozenset({0, 404, 502, 503})
ADOPT_MAX_ATTEMPTS = 4
ADOPT_RETRY_SLEEP_SECONDS = 3


def extract_instance_name(resp, fallback=None):
    """Instance name from a Template API response, or *fallback* if missing."""

    def pick(*vals):
        for val in vals:
            if isinstance(val, str) and val.strip():
                return val.strip()
        return None

    found = None
    if isinstance(resp, dict):
        data = resp.get("data") if isinstance(resp.get("data"), dict) else {}
        meta = resp.get("metadata") if isinstance(resp.get("metadata"), dict) else {}
        data_meta = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
        found = pick(
            resp.get("instanceName"),
            resp.get("name"),
            data.get("instanceName"),
            data.get("name"),
            data_meta.get("name"),
            meta.get("name"),
        )
    return found or pick(fallback)


def template_name_from_yaml(yaml_text):
    """Best-effort Template CR metadata.name from the first YAML document."""
    if not yaml_text:
        return None
    first = yaml_text.split("\n---", 1)[0]
    if not re.search(r"^kind:\s*Template\s*$", first, re.MULTILINE):
        return None
    meta = re.search(r"^metadata:\s*$", first, re.MULTILINE)
    if not meta:
        return None
    rest = first[meta.end() :]
    top = re.search(r"^[^:\s#][^:]*:", rest, re.MULTILINE)
    block = rest[: top.start()] if top else rest
    match = re.search(r"^  name:\s*[\"']?([^\"'\s]+)[\"']?\s*$", block, re.MULTILINE)
    if not match:
        return None
    name = match.group(1)
    if "${{" in name:
        return None
    return name


def _brain_adoption_skipped(reason, error=None, ok=None):
    return {
        "skipped": True,
        "reason": reason,
        "ok": ok,
        "status": None,
        "projectId": None,
        "warnings": [],
        "error": error,
    }


def _adoption_warnings(body):
    if not isinstance(body, dict):
        return []
    adoption = body.get("adoption") if isinstance(body.get("adoption"), dict) else {}
    warnings = adoption.get("warnings") or []
    return warnings if isinstance(warnings, list) else []


def _adoption_error(status, body):
    if isinstance(body, dict) and body.get("error"):
        return str(body["error"])
    if isinstance(body, str) and body.strip():
        return body.strip()[:500]
    return f"adopt-template-instance failed ({status})"


def _should_retry_adopt(status, body, attempt):
    if attempt >= ADOPT_MAX_ATTEMPTS:
        return False
    if status in ADOPT_RETRY_STATUSES:
        return True
    if status == 200 and "incompleteResourceSet" in _adoption_warnings(body):
        return True
    return False


def _adoption_result(status, body):
    warnings = _adoption_warnings(body)
    project = body.get("project") if isinstance(body, dict) else None
    project_id = project.get("id") if isinstance(project, dict) else None
    ok = status == 200
    return {
        "skipped": False,
        "reason": None,
        "ok": ok,
        "status": status,
        "projectId": project_id,
        "warnings": warnings,
        "error": None if ok else _adoption_error(status, body),
    }


def is_brain_managed_deploy():
    return bool(os.environ.get("SEALAI_DEPLOY_TASK_ID") or os.environ.get("SEALAI_PROJECT_ID"))


def is_brain_adoption_region(domain):
    """Brain only runs on international Sealos (*.sealos.io), not China (*.sealos.run)."""
    host = (domain or "").strip().lower().rstrip(".")
    return host == "sealos.io" or host.endswith(".sealos.io")


def maybe_adopt_template_instance(instance_name, template_name=None, dry_run=False):
    """POST Brain adopt-template-instance, or skip. HTTP failures are returned, not raised."""
    if dry_run:
        return _brain_adoption_skipped("dry-run")
    domain = resolve_region_domain()
    if domain is None:
        if is_brain_managed_deploy():
            return _brain_adoption_skipped("managed")
        return _brain_adoption_skipped(
            "unknown-region", error="cannot determine region; set SEALOS_REGION"
        )
    if not is_brain_adoption_region(domain):
        return _brain_adoption_skipped("not-sealos-io")
    if is_brain_managed_deploy():
        return _brain_adoption_skipped("managed")
    if not instance_name:
        return _brain_adoption_skipped(
            "missing-instance-name",
            error="could not determine instance name from the Template API response",
        )
    url = f"https://brain.{domain}/api/projects/adopt-template-instance"
    headers = {"Authorization": f"Bearer {api_credential()}"}
    body = {"instanceName": instance_name}
    if template_name:
        body["templateName"] = template_name
    status, resp = 0, None
    for attempt in range(1, ADOPT_MAX_ATTEMPTS + 1):
        status, resp = http_json(
            url, method="POST", headers=headers, data=body, timeout=60, fatal=False
        )
        if _should_retry_adopt(status, resp, attempt):
            time.sleep(ADOPT_RETRY_SLEEP_SECONDS)
            continue
        break
    return _adoption_result(status, resp)


def cmd_deploy(args):
    if not os.path.exists(args.template):
        fail("template file not found", path=args.template)
    with open(args.template) as f:
        yaml_text = f.read()

    deploy_args = parse_deploy_args(args)
    extra_labels = parse_extra_labels(args)
    credential = api_credential()
    url = f"{template_api_base()}/api/v2alpha/templates/raw"
    body = {"yaml": yaml_text, "args": deploy_args, "dryRun": bool(args.dry_run)}
    if extra_labels:
        body["extraLabels"] = extra_labels
    status, resp = http_json(
        url,
        method="POST",
        headers={"Authorization": credential},
        data=body,
        timeout=120,
    )
    ok = status in (200, 201)
    result = {
        "success": ok,
        "dry_run": bool(args.dry_run),
        "status": status,
        "deploy_url": url,
        "response": resp,
    }
    if ok:
        result["brain_adoption"] = maybe_adopt_template_instance(
            extract_instance_name(resp),
            template_name=template_name_from_yaml(yaml_text),
            dry_run=bool(args.dry_run),
        )
    print(json.dumps(result, indent=2))
    if not ok:
        sys.exit(1)


def parse_deploy_args(args):
    deploy_args = {}
    if args.args_json:
        try:
            deploy_args = json.loads(args.args_json)
        except ValueError:
            fail("--args-json is not valid JSON")
    elif args.args_file:
        with open(args.args_file) as f:
            deploy_args = json.load(f)
    if not isinstance(deploy_args, dict):
        fail("deploy args must be a JSON object")
    return deploy_args


def parse_loose_labels(raw):
    """Parse `{k:v,k:v}` — the shape a JSON object takes after a platform strips
    the quotes from an env value. Returns None when malformed."""
    text = (raw or "").strip()
    if len(text) < 2 or text[0] != "{" or text[-1] != "}":
        return None
    labels = {}
    for item in text[1:-1].split(","):
        item = item.strip()
        if not item:
            continue
        if ":" not in item:
            return None
        key, value = item.split(":", 1)
        key = key.strip().strip("\"'")
        value = value.strip().strip("\"'")
        if not key or not value:
            return None
        labels[key] = value
    return labels


def _validate_extra_labels(labels):
    if not isinstance(labels, dict) or not all(
        isinstance(k, str) and k and isinstance(v, str) for k, v in labels.items()
    ):
        fail("extra labels must be a JSON object of string values")
    return labels or None


def parse_extra_labels(args):
    """Ownership labels attached to every deployed resource.

    --labels-json > SEALAI_DEPLOY_LABELS_PATH (JSON file) > SEALAI_DEPLOY_LABELS_JSON.
    The env string also accepts the quote-stripped `{k:v,...}` form."""
    explicit = getattr(args, "labels_json", None)
    if explicit:
        try:
            return _validate_extra_labels(json.loads(explicit))
        except ValueError:
            fail("--labels-json is not valid JSON")
    path = os.environ.get("SEALAI_DEPLOY_LABELS_PATH")
    if path:
        try:
            with open(path) as f:
                return _validate_extra_labels(json.load(f))
        except OSError:
            fail("SEALAI_DEPLOY_LABELS_PATH file cannot be read", path=path)
        except ValueError:
            fail("SEALAI_DEPLOY_LABELS_PATH file is not valid JSON", path=path)
    raw = os.environ.get("SEALAI_DEPLOY_LABELS_JSON")
    if not raw or not raw.strip():
        return None
    try:
        labels = json.loads(raw)
    except ValueError:
        labels = parse_loose_labels(raw)
        if labels is None:
            fail("extra labels are neither valid JSON nor {k:v,...} form")
    return _validate_extra_labels(labels)


def cmd_deploy_store(args):
    import random
    import string

    name = args.name or (
        args.template + "-" + "".join(random.choices(string.ascii_lowercase, k=8))
    )
    credential = api_credential()
    url = f"{template_api_base()}/api/v2alpha/templates/instances"
    status, resp = http_json(
        url,
        method="POST",
        headers={"Authorization": credential},
        data={"name": name, "template": args.template, "args": parse_deploy_args(args)},
        timeout=120,
    )
    ok = status in (200, 201)
    result = {"success": ok, "status": status, "instance": name, "response": resp}
    if ok:
        result["brain_adoption"] = maybe_adopt_template_instance(
            extract_instance_name(resp, fallback=name),
            template_name=args.template,
        )
    print(json.dumps(result, indent=2))
    if not ok:
        sys.exit(1)


def cmd_adopt(args):
    result = maybe_adopt_template_instance(args.instance, template_name=args.template_name)
    print(json.dumps({"instance": args.instance, "brain_adoption": result}, indent=2))
    if result.get("skipped"):
        if result.get("error"):
            sys.exit(1)
        return
    if not result.get("ok"):
        sys.exit(1)


def cmd_store_get(args):
    base = template_api_base()
    status, resp = http_json(
        f"{base}/api/v2alpha/templates/{urllib.parse.quote(args.template)}",
        timeout=30,
    )
    if status != 200:
        fail(f"template lookup failed ({status})", response=str(resp)[:500])
    out = resp if isinstance(resp, dict) else {"detail": resp}
    if args.yaml:
        status, src = http_json(
            f"{base}/api/getTemplateSource"
            f"?templateName={urllib.parse.quote(args.template)}&includeReadme=false",
            headers={"Authorization": api_credential()},
            timeout=30,
        )
        if status == 200 and isinstance(src, dict):
            data = src.get("data") or {}
            out["templateYaml"] = data.get("templateYaml")
            out["appYaml"] = data.get("appYaml")
        else:
            out["yaml_error"] = f"getTemplateSource failed ({status})"
    print(json.dumps(out, indent=2))


def cmd_store_export(args):
    """Materialize a store template's source (Template CR + resources) into a
    single local file, ready for a raw deploy or a managed-mode handshake."""
    status, src = http_json(
        f"{template_api_base()}/api/getTemplateSource"
        f"?templateName={urllib.parse.quote(args.template)}&includeReadme=false",
        headers={"Authorization": api_credential()},
        timeout=30,
    )
    if status != 200 or not isinstance(src, dict):
        fail(f"getTemplateSource failed ({status})", response=str(src)[:500])
    data = src.get("data") or {}
    template_yaml = (data.get("templateYaml") or "").strip("\n")
    app_yaml = (data.get("appYaml") or "").strip("\n")
    if "app.sealos.io/v1" not in template_yaml or "kind: Template" not in template_yaml:
        fail("template source is missing the app.sealos.io/v1 Template header")
    if not app_yaml:
        fail("template source has no resource documents")
    combined = template_yaml + "\n---\n" + app_yaml + "\n"
    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
    with open(args.out, "w") as f:
        f.write(combined)
    print(
        json.dumps(
            {
                "success": True,
                "template": args.template,
                "out": args.out,
                "bytes": len(combined.encode()),
                "sha256": hashlib.sha256(combined.encode()).hexdigest(),
            },
            indent=2,
        )
    )


def cmd_instances(_args):
    status, resp = http_json(
        f"{template_api_base()}/api/instance/list",
        headers={"Authorization": api_credential()},
        timeout=30,
    )
    if status != 200:
        fail(f"instance list failed ({status})", response=str(resp)[:500])
    items = resp.get("data") if isinstance(resp, dict) else resp
    if isinstance(items, dict):
        items = items.get("items") or items
    out = []
    if isinstance(items, list):
        for it in items:
            meta = it.get("metadata") or {}
            spec = it.get("spec") or {}
            out.append(
                {
                    "name": meta.get("name"),
                    "template": spec.get("title") or spec.get("templateType"),
                    "created": meta.get("creationTimestamp"),
                }
            )
        print(json.dumps({"count": len(out), "instances": out}, indent=2))
    else:
        print(json.dumps(resp, indent=2))


def cmd_delete(args):
    credential = api_credential()
    url = f"{template_api_base()}/api/v2alpha/templates/instances/{urllib.parse.quote(args.instance)}"
    status, resp = http_json(
        url,
        method="DELETE",
        headers={"Authorization": credential},
        timeout=120,
    )
    ok = status in (200, 204)
    print(json.dumps({"success": ok, "status": status, "instance": args.instance, "response": resp or None}, indent=2))
    if not ok:
        sys.exit(1)


def cmd_store_list(args):
    status, resp = http_json(
        f"{template_api_base()}/api/listTemplate?language=en", timeout=30
    )
    if status != 200 or not isinstance(resp, dict):
        fail(f"listTemplate failed ({status})", response=str(resp)[:500])
    templates = (resp.get("data") or {}).get("templates") or []
    query = (args.search or "").lower()
    items = []
    for t in templates:
        meta = t.get("metadata") or {}
        spec = t.get("spec") or {}
        entry = {
            "name": meta.get("name"),
            "title": spec.get("title"),
            "description": (spec.get("description") or "")[:160],
            "categories": spec.get("categories") or [],
        }
        haystack = json.dumps(entry).lower()
        if not query or query in haystack:
            items.append(entry)
    print(json.dumps({"count": len(items), "templates": items}, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status")

    p = sub.add_parser("login")
    p.add_argument("--region", help=f"region URL (default {DEFAULT_REGION}); known: {', '.join(KNOWN_REGIONS)}")

    sub.add_parser("workspaces")

    p = sub.add_parser("switch")
    p.add_argument("workspace", help="workspace id, uid, or team name")

    p = sub.add_parser("deploy")
    p.add_argument("template", help="path to a Sealos template YAML")
    p.add_argument("--args-json", help="deploy inputs as a JSON object string")
    p.add_argument("--args-file", help="deploy inputs as a JSON file (use for secrets)")
    p.add_argument(
        "--labels-json",
        help="extra ownership labels as a JSON object string "
        "(default: SEALAI_DEPLOY_LABELS_PATH file, then SEALAI_DEPLOY_LABELS_JSON, "
        "which also accepts the quote-stripped {k:v,...} form)",
    )
    p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("deploy-store")
    p.add_argument("template", help="template-store template name")
    p.add_argument("--name", help="instance name (default: <template>-<random8>)")
    p.add_argument("--args-json", help="template inputs as a JSON object string")
    p.add_argument("--args-file", help="template inputs as a JSON file (use for secrets)")

    p = sub.add_parser("adopt")
    p.add_argument(
        "instance",
        help="template instance name to claim as a Brain Project (*.sealos.io only)",
    )
    p.add_argument(
        "--template-name",
        help="optional template name for the Brain Project display name",
    )

    p = sub.add_parser("store-list")
    p.add_argument("--search", help="case-insensitive filter")

    p = sub.add_parser("store-get")
    p.add_argument("template", help="template-store template name")
    p.add_argument("--yaml", action="store_true", help="include template source YAML")

    p = sub.add_parser("store-export")
    p.add_argument("template", help="template-store template name")
    p.add_argument("--out", required=True, help="output path for the combined YAML")

    sub.add_parser("instances")

    p = sub.add_parser("delete")
    p.add_argument("instance", help="instance name to delete (removes all its resources)")

    args = parser.parse_args()
    {
        "status": cmd_status,
        "login": cmd_login,
        "workspaces": cmd_workspaces,
        "switch": cmd_switch,
        "deploy": cmd_deploy,
        "deploy-store": cmd_deploy_store,
        "adopt": cmd_adopt,
        "store-list": cmd_store_list,
        "store-get": cmd_store_get,
        "store-export": cmd_store_export,
        "instances": cmd_instances,
        "delete": cmd_delete,
    }[args.command](args)


if __name__ == "__main__":
    main()
