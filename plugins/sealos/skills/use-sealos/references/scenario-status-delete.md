# Scenario: status / delete

## Status — what the user sounds like

- "What am I running?" / "is xxx still up?" / "what's on my account?"

## Delete — what the user sounds like

- "Delete xxx" / "take it down" / "I don't need this anymore" / "clean this
  up"

## Status

```bash
python3 scripts/sealos-api.py instances                  # deployed instances
kubectl get deploy,sts,pods                              # workloads
kubectl get clusters.apps.kubeblocks.io                  # databases + phase
kubectl get ingress -o custom-columns='NAME:.metadata.name,HOST:.spec.rules[0].host'
kubectl get resourcequota -o yaml                        # quota vs usage
```

Report in plain words: which apps, each one's state, its URL.

## Delete (destructive — confirm first)

1. Confirm with the user WHAT gets deleted (app / database / bucket).
2. Delete:
   ```bash
   python3 scripts/sealos-api.py delete <instance>            # the instance + everything in it
   kubectl delete cluster.apps.kubeblocks.io <name>          # a single database
   ```
3. Confirm it is actually gone: `kubectl get all -l
   "cloud.sealos.io/deploy-on-sealos=<instance>"` → empty.
4. Remind: idle PVCs and buckets still bill the account — offer to remove
   them too (only once their workload is already gone).

## How to reply

- Status → "You're running: xxx (up), xxx (starting)..." with URLs.
- Deleted → "Deleted — I verified it's fully gone." + the storage-cost
  reminder when PVCs or buckets remain.
