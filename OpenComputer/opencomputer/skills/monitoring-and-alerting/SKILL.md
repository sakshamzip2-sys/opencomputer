---
name: monitoring-and-alerting
description: Use when setting up metrics, dashboards, alert rules, SLIs, SLOs, prometheus queries, or oncall runbooks
---

# Monitoring & Alerting

## When to use

- New service going live
- Alert noise complaint ("I get paged for nothing")
- Designing SLIs/SLOs

## Steps

1. **Four golden signals.** Latency, traffic, errors, saturation. Every service. No exceptions.
2. **Define an SLO before alerts.** "99% of requests < 500ms over 30 days." Alerts trigger on burn rate against the SLO budget, not raw thresholds.
3. **Symptom-based alerts, not cause.** Alert on "user-visible 500s spiking", not "CPU at 90%". Causes are diagnostics, not pages.
4. **Page on actionable + urgent only.** "What does the on-call do at 3am?" If you don't have a runbook step, don't alert.
5. **Multi-window alerting.** Burn rate over 5min AND 1hr. Avoids both noisy-but-fast and slow-degradation alerts.
6. **Dashboards by audience.** Service-level for owners, exec dashboards for stakeholders. Don't mix.
7. **Synthetic checks.** External prober hitting a critical endpoint every minute. Catches "your monitoring is broken" failures.

## Notes

- Cardinality kills time-series databases. Don't put user_id in labels. Top-N exemplars instead.
- Histograms > averages. p50 + p99 tell different stories.
- `up == 0` (Prometheus) is the most underused alert: "this scrape is failing." Often the first sign of trouble.
