#!/usr/bin/env python3
"""Unit tests for sealos-api.py environment handling: region resolution,
Template API base URL, in-cluster kubeconfig inlining, and label parsing.

Mocks http_json; does not hit the network.
"""

import argparse
import base64
import contextlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
import urllib.parse
from unittest.mock import patch

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sealos-api.py")
SPEC = importlib.util.spec_from_file_location("sealos_api_env", SCRIPT)
api = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(api)

ENV_KEYS = (
    "SEALOS_REGION",
    "SEALAI_TEMPLATE_API_URL",
    "SEALAI_DEPLOY_LABELS_JSON",
    "SEALAI_DEPLOY_LABELS_PATH",
    "SEALAI_DEPLOY_TASK_ID",
    "SEALAI_PROJECT_ID",
)

DEVBOX_KUBECONFIG = """apiVersion: v1
kind: Config
clusters:
- cluster:
    certificate-authority: {ca}
    server: https://kubernetes.default.svc
  name: sealos
contexts:
- context:
    cluster: sealos
    namespace: ns-abcd1234
    user: sealos
  name: sealos
current-context: sealos
users:
- name: sealos
  user:
    tokenFile: {token}
"""

PUBLIC_KUBECONFIG = """apiVersion: v1
clusters:
- cluster:
    server: https://usw-1.sealos.io:6443
  name: sealos
contexts:
- context:
    cluster: sealos
    namespace: ns-public
    user: sealos
  name: sealos
current-context: sealos
users:
- name: sealos
  user:
    token: abc
"""

CA_BYTES = b"-----BEGIN CERTIFICATE-----\n\x00\xffMIIB\n-----END CERTIFICATE-----\n"
TOKEN_TEXT = "eyJhbGciOiJSUzI1NiJ9.devbox-token"


class FakeHttp:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def __call__(self, url, method="GET", headers=None, data=None, **_kwargs):
        self.calls.append(
            {"url": url, "method": method, "headers": dict(headers or {}), "data": data}
        )
        if not self._responses:
            raise AssertionError(f"unexpected http_json call: {method} {url}")
        return self._responses.pop(0)

    def brain_calls(self):
        return [c for c in self.calls if "/adopt-template-instance" in c["url"]]

    def template_calls(self):
        return [c for c in self.calls if "/adopt-template-instance" not in c["url"]]


def write_devbox_dir(tmp):
    ca = os.path.join(tmp, "ca.crt")
    token = os.path.join(tmp, "token")
    config = os.path.join(tmp, "config")
    with open(ca, "wb") as f:
        f.write(CA_BYTES)
    with open(token, "w") as f:
        f.write(TOKEN_TEXT + "\n")
    with open(config, "w") as f:
        f.write(DEVBOX_KUBECONFIG.format(ca=ca, token=token))
    return config


def deploy_args(template_path, **overrides):
    ns = argparse.Namespace(
        template=template_path, args_json=None, args_file=None, labels_json=None, dry_run=False
    )
    for k, v in overrides.items():
        setattr(ns, k, v)
    return ns


class EnvIsolatedTestCase(unittest.TestCase):
    def setUp(self):
        self._env = os.environ.copy()
        for key in ENV_KEYS:
            os.environ.pop(key, None)
        self._patches = []

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        os.environ.clear()
        os.environ.update(self._env)

    def start(self, p):
        self._patches.append(p)
        return p.start()

    def use_kubeconfig_text(self, text):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "kubeconfig")
        with open(path, "w") as f:
            f.write(text)
        self.start(patch.object(api, "load_auth", return_value={}))
        self.start(patch.object(api, "KUBECONFIG_PATH", path))

    def fail_json(self, fn, *args, **kwargs):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as caught:
            fn(*args, **kwargs)
        self.assertEqual(caught.exception.code, 1)
        return json.loads(err.getvalue())


class RegionResolutionTests(EnvIsolatedTestCase):
    def test_env_url_beats_auth_json(self):
        os.environ["SEALOS_REGION"] = "https://usw-1.sealos.io/"
        self.start(patch.object(api, "load_auth", return_value={"region": "https://gzg.sealos.run"}))
        self.assertEqual(api.resolve_region_domain(), "usw-1.sealos.io")

    def test_env_bare_host_beats_auth_json(self):
        os.environ["SEALOS_REGION"] = " USW-1.sealos.io "
        self.start(patch.object(api, "load_auth", return_value={"region": "https://gzg.sealos.run"}))
        self.assertEqual(api.resolve_region_domain(), "usw-1.sealos.io")

    def test_auth_json_beats_kubeconfig_server(self):
        self.use_kubeconfig_text(PUBLIC_KUBECONFIG)
        self.start(patch.object(api, "load_auth", return_value={"region": "https://gzg.sealos.run"}))
        self.assertEqual(api.resolve_region_domain(), "gzg.sealos.run")

    def test_kubeconfig_server_used_last(self):
        self.use_kubeconfig_text(PUBLIC_KUBECONFIG)
        self.assertEqual(api.resolve_region_domain(), "usw-1.sealos.io")
        self.assertEqual(api.region_domain(), "usw-1.sealos.io")

    def test_in_cluster_server_yields_none_and_region_domain_fails(self):
        self.use_kubeconfig_text(DEVBOX_KUBECONFIG.format(ca="/x/ca.crt", token="/x/token"))
        self.assertIsNone(api.resolve_region_domain())
        err = self.fail_json(api.region_domain)
        self.assertIn("SEALOS_REGION", err["error"])
        self.assertEqual(err["server"], "https://kubernetes.default.svc")

    def test_missing_kubeconfig_yields_none_without_failing(self):
        self.start(patch.object(api, "load_auth", return_value={}))
        self.start(patch.object(api, "KUBECONFIG_PATH", "/nonexistent/none"))
        self.assertIsNone(api.resolve_region_domain())
        err = self.fail_json(api.region_domain)
        self.assertIn("SEALOS_REGION", err["error"])
        self.assertNotIn("server", err)

    def test_is_in_cluster_host(self):
        for host in (
            "kubernetes",
            "kubernetes.default",
            "kubernetes.default.svc",
            "kubernetes.default.svc.cluster.local",
            "template-frontend.template-frontend.svc",
            "localhost",
            "10.96.0.1",
            "::1",
            "KUBERNETES.DEFAULT.SVC",
        ):
            self.assertTrue(api.is_in_cluster_host(host), host)
        for host in ("usw-1.sealos.io", "gzg.sealos.run", "svc.example.com", "", None):
            self.assertFalse(api.is_in_cluster_host(host), host)


class TemplateApiBaseTests(EnvIsolatedTestCase):
    def test_override_wins_even_when_in_cluster(self):
        self.use_kubeconfig_text(DEVBOX_KUBECONFIG.format(ca="/x/ca.crt", token="/x/token"))
        os.environ["SEALAI_TEMPLATE_API_URL"] = "http://template-frontend.template-frontend.svc:3000/"
        self.assertEqual(
            api.template_api_base(), "http://template-frontend.template-frontend.svc:3000"
        )
        self.assertEqual(
            api.template_api_base_or_none(), "http://template-frontend.template-frontend.svc:3000"
        )

    def test_region_derived_base(self):
        os.environ["SEALOS_REGION"] = "https://usw-1.sealos.io"
        self.assertEqual(api.template_api_base(), "https://template.usw-1.sealos.io")

    def test_or_none_when_unresolvable(self):
        self.use_kubeconfig_text(DEVBOX_KUBECONFIG.format(ca="/x/ca.crt", token="/x/token"))
        self.assertIsNone(api.template_api_base_or_none())


class PortableKubeconfigTests(EnvIsolatedTestCase):
    def test_devbox_shape_inlined(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = write_devbox_dir(tmp)
            with open(config) as f:
                text = f.read()
            out = api.portable_kubeconfig(text, tmp)
        expected_ca = base64.b64encode(CA_BYTES).decode()
        self.assertIn(f"\n    certificate-authority-data: {expected_ca}\n", out)
        self.assertIn(f"\n    token: {TOKEN_TEXT}\n", out)
        self.assertNotIn("certificate-authority:", out)
        self.assertNotIn("tokenFile", out)
        self.assertIn("    server: https://kubernetes.default.svc\n", out)
        self.assertIn("    namespace: ns-abcd1234\n", out)
        self.assertEqual(len(out.splitlines()), len(text.splitlines()))

    def test_relative_paths_resolved_against_base_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_devbox_dir(tmp)
            text = DEVBOX_KUBECONFIG.format(ca="ca.crt", token="./token")
            out = api.portable_kubeconfig(text, tmp)
        self.assertIn(base64.b64encode(CA_BYTES).decode(), out)
        self.assertIn(f"token: {TOKEN_TEXT}\n", out)

    def test_quoted_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_devbox_dir(tmp)
            text = DEVBOX_KUBECONFIG.format(
                ca=f'"{os.path.join(tmp, "ca.crt")}"', token=f"'{os.path.join(tmp, 'token')}'"
            )
            out = api.portable_kubeconfig(text, tmp)
        self.assertIn(base64.b64encode(CA_BYTES).decode(), out)
        self.assertIn(f"token: {TOKEN_TEXT}\n", out)

    def test_already_inlined_returned_unchanged(self):
        text = (
            "clusters:\n- cluster:\n    certificate-authority-data: QUJD\n"
            "    server: https://usw-1.sealos.io:6443\nusers:\n- name: u\n  user:\n    token: abc\n"
        )
        self.assertIs(api.portable_kubeconfig(text, "/nowhere"), text)
        self.assertFalse(api.kubeconfig_has_file_refs(text))

    def test_client_cert_and_key_inlined(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name, content in (("c.crt", b"CERT"), ("c.key", b"KEY")):
                with open(os.path.join(tmp, name), "wb") as f:
                    f.write(content)
            text = "users:\n- name: u\n  user:\n    client-certificate: c.crt\n    client-key: c.key\n"
            out = api.portable_kubeconfig(text, tmp)
        self.assertEqual(
            out,
            "users:\n- name: u\n  user:\n"
            f"    client-certificate-data: {base64.b64encode(b'CERT').decode()}\n"
            f"    client-key-data: {base64.b64encode(b'KEY').decode()}\n",
        )

    def test_missing_token_file_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "ca.crt"), "wb") as f:
                f.write(CA_BYTES)
            text = DEVBOX_KUBECONFIG.format(ca="ca.crt", token="missing-token")
            err = self.fail_json(api.portable_kubeconfig, text, tmp)
        self.assertEqual(err["key"], "tokenFile")
        self.assertTrue(err["path"].endswith("missing-token"))


class DeployHeaderTests(EnvIsolatedTestCase):
    def _run_deploy(self, http, config_path, template_body="kind: Template\nmetadata:\n  name: demo\n"):
        self.start(patch.object(api, "load_auth", return_value={}))
        self.start(patch.object(api, "KUBECONFIG_PATH", config_path))
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "t.yaml")
            with open(path, "w") as f:
                f.write(template_body)
            buf = io.StringIO()
            with patch.object(api, "http_json", http), contextlib.redirect_stdout(buf):
                api.cmd_deploy(deploy_args(path))
        return json.loads(buf.getvalue())

    def test_devbox_kubeconfig_header_is_self_contained(self):
        os.environ["SEALOS_REGION"] = "https://usw-1.sealos.io"
        os.environ["SEALAI_DEPLOY_TASK_ID"] = "t"
        http = FakeHttp([(201, {"name": "demo-abc"})])
        with tempfile.TemporaryDirectory() as tmp:
            config = write_devbox_dir(tmp)
            out = self._run_deploy(http, config)
        self.assertTrue(out["success"])
        self.assertEqual(len(http.template_calls()), 1)
        call = http.template_calls()[0]
        self.assertEqual(call["url"], "https://template.usw-1.sealos.io/api/v2alpha/templates/raw")
        self.assertEqual(out["deploy_url"], call["url"])
        header = urllib.parse.unquote(call["headers"]["Authorization"])
        self.assertIn(f"token: {TOKEN_TEXT}", header)
        self.assertIn("certificate-authority-data:", header)
        self.assertNotIn("tokenFile", header)
        self.assertNotIn(tmp, header)
        self.assertEqual(out["brain_adoption"]["reason"], "managed")
        self.assertEqual(http.brain_calls(), [])

    def test_managed_in_cluster_with_template_override_and_no_region(self):
        os.environ["SEALAI_TEMPLATE_API_URL"] = "http://template-frontend.template-frontend.svc:3000"
        os.environ["SEALAI_DEPLOY_TASK_ID"] = "t"
        http = FakeHttp([(201, {"name": "demo-abc"})])
        with tempfile.TemporaryDirectory() as tmp:
            config = write_devbox_dir(tmp)
            out = self._run_deploy(http, config)
        self.assertTrue(out["success"])
        self.assertEqual(
            http.template_calls()[0]["url"],
            "http://template-frontend.template-frontend.svc:3000/api/v2alpha/templates/raw",
        )
        self.assertEqual(out["brain_adoption"]["reason"], "managed")
        self.assertEqual(http.brain_calls(), [])

    def test_unmanaged_in_cluster_without_region_fails_before_network(self):
        http = FakeHttp([])
        with tempfile.TemporaryDirectory() as tmp:
            config = write_devbox_dir(tmp)
            self.start(patch.object(api, "load_auth", return_value={}))
            self.start(patch.object(api, "KUBECONFIG_PATH", config))
            with patch.object(api, "http_json", http):
                err = self.fail_json(api.cmd_instances, argparse.Namespace())
        self.assertIn("SEALOS_REGION", err["error"])
        self.assertEqual(http.calls, [])

    def test_unmanaged_deploy_with_override_skips_adoption_as_unknown_region(self):
        os.environ["SEALAI_TEMPLATE_API_URL"] = "http://template-frontend.template-frontend.svc:3000"
        http = FakeHttp([(201, {"name": "demo-abc"})])
        with tempfile.TemporaryDirectory() as tmp:
            config = write_devbox_dir(tmp)
            out = self._run_deploy(http, config)
        self.assertTrue(out["success"])
        self.assertEqual(out["brain_adoption"]["reason"], "unknown-region")
        self.assertIn("SEALOS_REGION", out["brain_adoption"]["error"])
        self.assertEqual(http.brain_calls(), [])


class LabelsTests(EnvIsolatedTestCase):
    LOOSE = "{brain.io/managed-by:brain,brain.io/project-id:abc-123,brain.io/deployment-kind:template}"
    LOOSE_DICT = {
        "brain.io/managed-by": "brain",
        "brain.io/project-id": "abc-123",
        "brain.io/deployment-kind": "template",
    }

    def _args(self, labels_json=None):
        return argparse.Namespace(labels_json=labels_json)

    def test_cli_beats_env(self):
        os.environ["SEALAI_DEPLOY_LABELS_JSON"] = '{"a": "env"}'
        self.assertEqual(api.parse_extra_labels(self._args('{"a": "cli"}')), {"a": "cli"})

    def test_path_beats_json_env(self):
        os.environ["SEALAI_DEPLOY_LABELS_JSON"] = '{"a": "env"}'
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "labels.json")
            with open(path, "w") as f:
                json.dump({"a": "file"}, f)
            os.environ["SEALAI_DEPLOY_LABELS_PATH"] = path
            self.assertEqual(api.parse_extra_labels(self._args()), {"a": "file"})

    def test_strict_json_env(self):
        os.environ["SEALAI_DEPLOY_LABELS_JSON"] = json.dumps(self.LOOSE_DICT)
        self.assertEqual(api.parse_extra_labels(self._args()), self.LOOSE_DICT)

    def test_loose_env_form(self):
        os.environ["SEALAI_DEPLOY_LABELS_JSON"] = self.LOOSE
        self.assertEqual(api.parse_extra_labels(self._args()), self.LOOSE_DICT)

    def test_empty_object_is_none(self):
        os.environ["SEALAI_DEPLOY_LABELS_JSON"] = "{}"
        self.assertIsNone(api.parse_extra_labels(self._args()))

    def test_nothing_set_is_none(self):
        self.assertIsNone(api.parse_extra_labels(self._args()))

    def test_garbage_env_fails(self):
        os.environ["SEALAI_DEPLOY_LABELS_JSON"] = "not-labels"
        err = self.fail_json(api.parse_extra_labels, self._args())
        self.assertIn("neither valid JSON", err["error"])

    def test_missing_labels_path_fails(self):
        os.environ["SEALAI_DEPLOY_LABELS_PATH"] = "/nonexistent/labels.json"
        err = self.fail_json(api.parse_extra_labels, self._args())
        self.assertEqual(err["path"], "/nonexistent/labels.json")

    def test_non_string_values_fail(self):
        os.environ["SEALAI_DEPLOY_LABELS_JSON"] = '{"a": 1}'
        self.fail_json(api.parse_extra_labels, self._args())

    def test_parse_loose_labels_direct(self):
        self.assertEqual(api.parse_loose_labels(self.LOOSE), self.LOOSE_DICT)
        self.assertEqual(api.parse_loose_labels(" {} "), {})
        self.assertEqual(api.parse_loose_labels("{a:b,}"), {"a": "b"})
        self.assertEqual(api.parse_loose_labels('{ "a" : \'b\' }'), {"a": "b"})
        self.assertEqual(api.parse_loose_labels("{k:v:w}"), {"k": "v:w"})
        for bad in ("", "a:b", "{a}", "{:b}", "{a:}", "{a:b", "not-labels"):
            self.assertIsNone(api.parse_loose_labels(bad), bad)


class StatusTests(EnvIsolatedTestCase):
    def _status(self, config):
        self.start(patch.object(api, "load_auth", return_value={}))
        self.start(patch.object(api, "KUBECONFIG_PATH", config))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            api.cmd_status(argparse.Namespace())
        return json.loads(buf.getvalue())

    def test_token_file_kubeconfig(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = write_devbox_dir(tmp)
            out = self._status(config)
            self.assertTrue(out["authenticated"])
            self.assertEqual(out["namespace"], "ns-abcd1234")
            self.assertEqual(out["server"], "https://kubernetes.default.svc")
            self.assertIsNone(out["region_domain"])
            self.assertIsNone(out["template_api"])
            self.assertTrue(out["credential_files_inlined"])
            self.assertNotIn(TOKEN_TEXT, json.dumps(out))

            os.environ["SEALOS_REGION"] = "https://usw-1.sealos.io"
            out = self._status(config)
            self.assertEqual(out["region_domain"], "usw-1.sealos.io")
            self.assertEqual(out["template_api"], "https://template.usw-1.sealos.io")

    def test_public_kubeconfig(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = os.path.join(tmp, "kubeconfig")
            with open(config, "w") as f:
                f.write(PUBLIC_KUBECONFIG)
            out = self._status(config)
        self.assertTrue(out["authenticated"])
        self.assertEqual(out["region_domain"], "usw-1.sealos.io")
        self.assertFalse(out["credential_files_inlined"])

    def test_missing_kubeconfig(self):
        out = self._status("/nonexistent/kubeconfig")
        self.assertEqual(out, {"authenticated": False})


if __name__ == "__main__":
    unittest.main()
