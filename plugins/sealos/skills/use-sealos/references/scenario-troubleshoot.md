# Scenario: something is broken

## What the user sounds like

- "My site won't open" / "it's down" / "white screen"
- "It errors" / "keeps 500ing" / "it's slow"
- "It can't reach the database" / "it worked yesterday"

## Your goal

Find the failure, fix it, verify it is really back.

## Steps

1. **Pin down WHICH app**: ask which URL is failing. The host the user
   reports may be their own custom domain, not the subdomain we assigned —
   resolve it against Ingress rules:
   ```bash
   kubectl get ingress -o custom-columns='HOST:.spec.rules[0].host,NAME:.metadata.name'
   ```
2. **Gather evidence**:
   ```bash
   kubectl get pods
   kubectl describe pod <pod>
   kubectl logs <pod> --all-containers --tail=100
   kubectl logs <pod> -p --tail=50
   kubectl get events --field-selector type=Warning --sort-by=.lastTimestamp | tail -20
   ```
3. **Match the symptom**:
   - URL 503/404 while the pod is ready → Ingress port ≠ Service port, or
     the app binds 127.0.0.1. Test in-pod
     (`kubectl exec <pod> -- wget -qO- localhost:<port>`, or curl): works
     in-pod → the wiring is wrong, not the app.
   - 500s → read the logs; a classic is root-owned files the app user can't
     rewrite → chown the app dirs as the LAST init step, and read the app's
     own error log inside the pod, not just `kubectl logs`.
   - Slow → under-resourced; move one ladder step up ([platform.md](platform.md)).
   - CrashLoopBackOff → usually missing env, or the database is still
     starting (normal for the first ~2 min; it self-heals).
   - ImagePullBackOff / `exec format error` → typo'd tag, private image
     without a pull secret, or an arm64 build → rebuild with
     `--platform linux/amd64`.
   - Pod `Pending` → quota exceeded or unbound PVC → `kubectl describe pod`;
     lower one ladder step or free quota.
   - Database stuck → `kubectl get cluster <name> -o yaml | grep -A5 status:`;
     clusters take 1-3 min to reach `Running`.
4. **Fix**:
   ```bash
   kubectl set image deploy/<name> <container>=<image>
   kubectl set env deploy/<name> KEY=value
   kubectl scale deploy/<name> --replicas=N
   ```
   Fold the fix into the template file — live-only fixes evaporate on the
   next deploy.
5. **Verify**: `bash scripts/wait-app.sh -u https://<host> deployment/<name>`.

## How to reply

- Fixed → "Found it and fixed it — try again."
- Need something from the user → "I need X from you."
- No technical post-mortem unless the user asks.
