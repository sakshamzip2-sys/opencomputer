---
name: kubernetes-troubleshoot
description: Use when diagnosing pod crashes, kubectl errors, deployment failures, CrashLoopBackOff, or k8s networking issues
---

# Kubernetes Troubleshooting

## When to use

- Pod is in `CrashLoopBackOff` / `ImagePullBackOff` / `Pending`
- Service can't reach a pod
- Deploy went out and something broke

## Steps

1. **`kubectl describe pod <name>`.** Events at the bottom tell you most stories — image pull failure, OOMKilled, scheduler couldn't fit.
2. **`kubectl logs <pod> --previous`.** The flag is critical for crashed containers — without it, you see the new pod's logs only.
3. **`Pending` = scheduler can't place.** Usually node resources or taints. `kubectl describe pod` events name the constraint.
4. **`CrashLoopBackOff` = app fails on start.** `--previous` logs + check liveness probe (kills if it fails).
5. **`ImagePullBackOff` = registry / auth.** Check image name typo, registry creds (`imagePullSecrets`), and registry availability.
6. **Network: pod-to-pod first, service second, ingress last.** `kubectl exec` into one pod, `curl` another pod's IP. If that works, the problem is the service or ingress, not the network.
7. **Resource limits.** OOMKilled (137 exit) = memory limit too low. CPU throttling = limits too low for actual usage.

## Notes

- `kubectl get events --sort-by=.lastTimestamp` is the cluster-wide log of recent failures.
- Don't `kubectl apply` straight to prod; use `--dry-run=server` first.
- Rolling deployment hung at "Available: 0"? Readiness probe is failing. Check it.
