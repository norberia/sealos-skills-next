# Scenario: status / delete

## Status — what the user sounds like

- "What am I running?" / "is xxx still up?" / "what's on my account?"

## Delete — what the user sounds like

- "Delete xxx" / "take it down" / "I don't need this anymore" / "clean this
  up"

## Status

```bash
export KUBECONFIG=~/.sealos/kubeconfig                 # once per session
python3 scripts/sealos-api.py instances                # deployed template instances
kubectl get deploy,sts,pods                            # workloads
kubectl get clusters.apps.kubeblocks.io                # databases + phase
kubectl get ingress -o custom-columns='NAME:.metadata.name,HOST:.spec.rules[0].host'
kubectl get all,ingress,cluster -l "cloud.sealos.io/deploy-on-sealos=<instance>"   # one instance's resources
kubectl get resourcequota -o yaml                      # namespace quota vs usage
```

Report in plain words: which apps, each one's state, its URL.

## Delete (destructive — confirm first)

1. Confirm with the user WHAT gets deleted (app / database / bucket).
2. Delete:
   ```bash
   python3 scripts/sealos-api.py delete <instance>            # instance + all resources
   kubectl delete cluster.apps.kubeblocks.io <name>           # a single database
   kubectl get pvc                                            # storage costs money even when idle
   kubectl delete pvc <name>                                  # only when its workload is gone
   ```
   Instance deletion sweeps everything carrying the instance label —
   including databases and StatefulSet PVCs.
3. Confirm it is actually gone: `kubectl get all -l
   "cloud.sealos.io/deploy-on-sealos=<instance>"` → empty.
4. Cost hygiene: after test deployments, delete the instance and check for
   orphaned PVCs. When the user reports unexpected cost, run
   `kubectl get deploy,sts,cluster,pvc` and look for forgotten
   experiments — running pods, clusters, PVCs, and buckets all bill the
   account.

## How to reply

- Status → "You're running: xxx (up), xxx (starting)..." with URLs.
- Deleted → "Deleted — I verified it's fully gone." + the storage-cost
  reminder when PVCs or buckets remain.
