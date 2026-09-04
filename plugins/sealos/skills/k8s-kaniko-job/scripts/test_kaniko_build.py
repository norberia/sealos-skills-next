#!/usr/bin/env python3
"""Unit tests for kaniko-build.py quota fitting and fail-fast classification.

Pure functions only: no kubectl, no network.
Run: python3 -m unittest discover -s . -p 'test_*.py' -v
"""

import contextlib
import importlib.util
import io
import json
import os
import unittest
from unittest.mock import patch

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kaniko-build.py")
SPEC = importlib.util.spec_from_file_location("kaniko_build", SCRIPT)
kb = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(kb)


def quota(name, hard, used):
    return {
        "metadata": {"name": name},
        "spec": {"hard": hard},
        "status": {"hard": hard, "used": used},
    }


def pod(phase="Pending", waiting=None, terminated=None, conditions=None, created="2026-09-04T03:00:00Z"):
    container = {"name": "kaniko", "state": {}}
    if waiting:
        container["state"]["waiting"] = waiting
    if terminated:
        container["state"]["terminated"] = terminated
    return {
        "metadata": {"name": "kaniko-app-1-xyz", "creationTimestamp": created},
        "status": {"phase": phase, "conditions": conditions or [], "containerStatuses": [container]},
    }


def job(conditions=None):
    return {"metadata": {"name": "kaniko-app-1"}, "status": {"conditions": conditions or []}}


T0 = kb.parse_k8s_timestamp("2026-09-04T03:00:00Z")


class QuantityTests(unittest.TestCase):
    def test_parse_memory_suffixes(self):
        self.assertEqual(kb.parse_quantity("1Gi", "memory"), 1024**3)
        self.assertEqual(kb.parse_quantity("512Mi", "memory"), 512 * 1024**2)
        self.assertEqual(kb.parse_quantity("1G", "memory"), 10**9)
        self.assertEqual(kb.parse_quantity("1e9", "memory"), 10**9)
        self.assertEqual(kb.parse_quantity("1.5Gi", "memory"), 3 * 1024**3 // 2)
        self.assertEqual(kb.parse_quantity("0", "memory"), 0)

    def test_parse_cpu_millicores(self):
        self.assertEqual(kb.parse_quantity("500m", "cpu"), 500)
        self.assertEqual(kb.parse_quantity("2", "cpu"), 2000)
        self.assertEqual(kb.parse_quantity("0.25", "cpu"), 250)
        self.assertEqual(kb.parse_quantity("1500m", "cpu"), 1500)

    def test_parse_rejects_garbage(self):
        for bad in ("", "3X", "Gi", "1..5", "1 Gi", "-1"):
            with self.assertRaises(ValueError, msg=bad):
                kb.parse_quantity(bad, "memory")

    def test_format_roundtrip(self):
        for text, dimension in [("8Gi", "memory"), ("3584Mi", "memory"), ("500m", "cpu"), ("2", "cpu"), ("1Gi", "ephemeral-storage")]:
            self.assertEqual(kb.format_quantity(kb.parse_quantity(text, dimension), dimension), text)
        self.assertEqual(kb.format_quantity(3 * 1024**3 // 2, "memory"), "1536Mi")
        self.assertEqual(kb.format_quantity(1000, "memory"), "1000")


class QuotaRemainingTests(unittest.TestCase):
    def test_hard_minus_used_and_alias_keys(self):
        remaining = kb.quota_remaining([
            quota("q", {"limits.memory": "4Gi", "memory": "4Gi", "cpu": "2", "pods": "10"},
                  {"limits.memory": "3Gi", "memory": "2Gi", "cpu": "1500m", "pods": "3"}),
        ])
        self.assertEqual(remaining["limits.memory"]["remaining"], 1024**3)
        self.assertEqual(remaining["requests.memory"]["remaining"], 2 * 1024**3)
        self.assertEqual(remaining["requests.cpu"]["remaining"], 500)
        self.assertNotIn("pods", remaining)
        self.assertNotIn("requests.pods", remaining)

    def test_tightest_quota_wins_and_negative_clamps(self):
        remaining = kb.quota_remaining([
            quota("loose", {"limits.memory": "16Gi"}, {"limits.memory": "1Gi"}),
            quota("tight", {"limits.memory": "4Gi"}, {"limits.memory": "5Gi"}),
        ])
        self.assertEqual(remaining["limits.memory"]["remaining"], 0)
        self.assertEqual(remaining["limits.memory"]["quota"], "tight")

    def test_spec_hard_when_status_missing(self):
        remaining = kb.quota_remaining([{"metadata": {"name": "q"}, "spec": {"hard": {"limits.cpu": "4"}}}])
        self.assertEqual(remaining["limits.cpu"], {"hard": 4000, "used": 0, "remaining": 4000, "quota": "q"})

    def test_no_quota(self):
        self.assertEqual(kb.quota_remaining([]), {})


class FitResourcesTests(unittest.TestCase):
    def fit(self, hard, used, overrides=None):
        return kb.fit_resources(kb.quota_remaining([quota("q", hard, used)]), overrides)

    def test_defaults_without_quota(self):
        resources = kb.fit_resources({})
        self.assertEqual(resources["limits"], {"cpu": "2", "memory": "8Gi", "ephemeral-storage": "10Gi"})
        self.assertEqual(resources["requests"], {"cpu": "500m", "memory": "2Gi", "ephemeral-storage": "2Gi"})
        self.assertEqual(resources["adjusted"], [])

    def test_sealos_tenant_quota_shrinks_memory_limit(self):
        # The Langfuse case: limits.memory=4Gi hard; the old 8Gi limit could never be admitted.
        resources = self.fit(
            {"limits.memory": "4Gi", "limits.cpu": "4", "requests.memory": "4Gi", "requests.cpu": "4"},
            {"limits.memory": "1Gi", "limits.cpu": "1", "requests.memory": "1Gi", "requests.cpu": "1"},
        )
        self.assertEqual(resources["limits"]["memory"], "3Gi")
        self.assertEqual(resources["limits"]["cpu"], "2")
        self.assertEqual(resources["requests"]["memory"], "2Gi")
        self.assertEqual(resources["limits"]["ephemeral-storage"], "10Gi")
        self.assertEqual(resources["adjusted"], ["limits.memory"])

    def test_request_never_exceeds_limit(self):
        resources = self.fit({"limits.memory": "4Gi"}, {"limits.memory": "2560Mi"})
        self.assertEqual(resources["limits"]["memory"], "1536Mi")
        self.assertEqual(resources["requests"]["memory"], "1536Mi")

    def test_request_quota_shrinks_request_independently(self):
        resources = self.fit({"requests.memory": "4Gi"}, {"requests.memory": "3Gi"})
        self.assertEqual(resources["requests"]["memory"], "1Gi")
        self.assertEqual(resources["limits"]["memory"], "8Gi")
        self.assertEqual(resources["adjusted"], ["requests.memory"])

    def test_below_limit_floor_fails_with_quota_numbers(self):
        with self.assertRaises(kb.ResourceFitError) as ctx:
            self.fit({"limits.memory": "4Gi"}, {"limits.memory": "3584Mi"})
        self.assertIn("limits.memory remaining 512Mi < floor 1Gi", str(ctx.exception))
        self.assertEqual(
            ctx.exception.quota,
            {"limits.memory": {"quota": "q", "hard": "4Gi", "used": "3584Mi", "remaining": "512Mi"}},
        )

    def test_below_request_floor_fails(self):
        with self.assertRaises(kb.ResourceFitError) as ctx:
            self.fit({"requests.cpu": "1"}, {"requests.cpu": "950m"})
        self.assertIn("requests.cpu remaining 50m < floor 100m", str(ctx.exception))

    def test_override_is_exact_and_may_exceed_default_ceiling(self):
        resources = kb.fit_resources({}, {"memory": "16Gi", "cpu": "250m"})
        self.assertEqual(resources["limits"]["memory"], "16Gi")
        self.assertEqual(resources["limits"]["cpu"], "250m")
        self.assertEqual(resources["requests"]["cpu"], "250m")

    def test_override_that_does_not_fit_fails_instead_of_shrinking(self):
        with self.assertRaises(kb.ResourceFitError) as ctx:
            self.fit({"limits.memory": "4Gi"}, {"limits.memory": "2Gi"}, {"memory": "3Gi"})
        self.assertIn("limits.memory remaining 2Gi < requested 3Gi", str(ctx.exception))

    def test_render_reflects_fitted_resources(self):
        resources = kb.fit_resources({}, {"memory": "3Gi"})
        manifest = kb.render_job(
            job_name="kaniko-x", namespace="ns-x", service_account=None, kaniko_image="k", platform="linux/amd64",
            context_uri="s3://b/k", dockerfile="Dockerfile", target_image="ghcr.io/o/r:t", s3_endpoint="http://s3:1319",
            aws_region="r", registry_secret="sec", s3_env_lines=[], build_args=[], deadline_seconds=600,
            resources=resources,
        )
        self.assertIn('memory: "3Gi"', manifest)
        self.assertNotIn('memory: "8Gi"', manifest)
        self.assertIn('ephemeral-storage: "10Gi"', manifest)


class ClassifyJobStateTests(unittest.TestCase):
    def classify(self, j=None, pods=(), events=(), now=T0):
        return kb.classify_job_state(j or job(), list(pods), list(events), now)

    def test_complete(self):
        state, _, _ = self.classify(job([{"type": "Complete", "status": "True"}]), [pod("Succeeded")])
        self.assertEqual(state, "complete")

    def test_job_failed_condition(self):
        state, reason, detail = self.classify(
            job([{"type": "Failed", "status": "True", "reason": "BackoffLimitExceeded", "message": "Job has reached the specified backoff limit"}]),
            [pod("Failed")],
        )
        self.assertEqual((state, reason), ("failed", "job_failed"))
        self.assertIn("BackoffLimitExceeded", detail)

    def test_pod_failed_before_job_condition(self):
        # kaniko exits 1 on a base image it cannot pull; the Job condition lags behind.
        state, reason, detail = self.classify(pods=[pod("Failed", terminated={"exitCode": 1, "reason": "Error"})])
        self.assertEqual((state, reason), ("failed", "pod_failed"))
        self.assertIn("exited 1", detail)

    def test_image_pull_backoff(self):
        for waiting_reason in ("ErrImagePull", "ImagePullBackOff"):
            state, reason, detail = self.classify(
                pods=[pod("Pending", waiting={"reason": waiting_reason, "message": "pull access denied"})]
            )
            self.assertEqual((state, reason), ("failed", "image_pull"), waiting_reason)
            self.assertIn(waiting_reason, detail)

    def test_create_container_error(self):
        state, reason, _ = self.classify(pods=[pod("Pending", waiting={"reason": "CreateContainerConfigError", "message": "secret not found"})])
        self.assertEqual((state, reason), ("failed", "container_create"))

    def test_container_creating_keeps_waiting(self):
        state, _, _ = self.classify(pods=[pod("Pending", waiting={"reason": "ContainerCreating"})])
        self.assertEqual(state, "waiting")

    def test_unschedulable_respects_grace(self):
        unschedulable = [{"type": "PodScheduled", "status": "False", "reason": "Unschedulable", "message": "0/3 nodes are available: Insufficient memory"}]
        state, _, _ = self.classify(pods=[pod("Pending", conditions=unschedulable)], now=T0 + kb.PENDING_GRACE_SECONDS - 1)
        self.assertEqual(state, "waiting")
        state, reason, detail = self.classify(pods=[pod("Pending", conditions=unschedulable)], now=T0 + kb.PENDING_GRACE_SECONDS)
        self.assertEqual((state, reason), ("failed", "unschedulable"))
        self.assertIn("Insufficient memory", detail)

    def test_failed_create_event_without_pods(self):
        events = [{"reason": "FailedCreate", "message": 'Error creating: pods "kaniko-x" is forbidden: exceeded quota: quota, requested: limits.memory=8Gi, used: limits.memory=1Gi, limited: limits.memory=4Gi'}]
        state, reason, detail = self.classify(events=events)
        self.assertEqual((state, reason), ("failed", "failed_create"))
        self.assertIn("exceeded quota", detail)

    def test_failed_create_event_ignored_once_pod_exists(self):
        events = [{"reason": "FailedCreate", "message": "transient"}]
        state, _, _ = self.classify(pods=[pod("Running")], events=events)
        self.assertEqual(state, "waiting")

    def test_running_pod_waits(self):
        state, _, _ = self.classify(pods=[pod("Running")], events=[{"reason": "SuccessfulCreate"}])
        self.assertEqual(state, "waiting")


class WaitForJobTests(unittest.TestCase):
    """Drive wait_for_job with a scripted kubectl to prove it stops early."""

    def run_wait(self, snapshots, wait_timeout=1800):
        polls = {"n": -1}

        def fake_kubectl(args, input_text=None, timeout=120):
            if args[1] == "job":
                polls["n"] += 1
            j, pods, events = snapshots[min(polls["n"], len(snapshots) - 1)]
            if args[1] == "job":
                return 0, json.dumps(j), ""
            if args[1] == "pods":
                return 0, json.dumps({"items": pods}), ""
            return 0, json.dumps({"items": events}), ""

        with patch.object(kb, "kubectl", side_effect=fake_kubectl), \
                patch.object(kb.time, "sleep") as sleep, \
                contextlib.redirect_stderr(io.StringIO()):
            result = kb.wait_for_job("ns", "kaniko-x", wait_timeout, poll_interval=1)
        return result, sleep.call_count

    def test_success_returns_none(self):
        result, sleeps = self.run_wait([
            (job(), [pod("Running")], []),
            (job([{"type": "Complete", "status": "True"}]), [pod("Succeeded")], []),
        ])
        self.assertIsNone(result)
        self.assertEqual(sleeps, 1)

    def test_fails_fast_on_failed_create(self):
        result, sleeps = self.run_wait([
            (job(), [], [{"reason": "FailedCreate", "message": "exceeded quota"}]),
        ])
        self.assertEqual(result[0], "failed_create")
        self.assertEqual(sleeps, 0)

    def test_times_out_at_deadline(self):
        monotonic = iter([0, 0, 1, 1, 2, 2, 3, 3, 4, 4])
        with patch.object(kb.time, "monotonic", side_effect=lambda: next(monotonic)):
            result, _ = self.run_wait([(job(), [pod("Running")], [])], wait_timeout=2)
        self.assertEqual(result[0], "timeout")


if __name__ == "__main__":
    unittest.main()
