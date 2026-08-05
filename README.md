# helm-charts-hostedclusters-setup

> **This chart currently contains the DHCP scope integration only.** The
> `HostedCluster` / `NodePool` resources that provision the cluster itself live in
> the air-gapped GitLab copy of this chart (`redbull/helm-charts/hostedclusters-setup`)
> and have not been mirrored here yet. Deploying this chart as-is creates a DHCP
> scope and **no hosted cluster**. See [Convergence](#convergence).

Rendered once per hosted cluster by the `gitops-day1` ApplicationSet cascade, from
a single values file in the values repo.

| | |
|---|---|
| Values repo | `team-redbull/day1` (GitHub) · `gitops-day1/platform-config` (GitLab) |
| Rendered by | `argocd-platform/hostedClusters/templates/hcAppset.yaml`, tier 3 |
| Field reference | `docs/dhcp_values.md` in `team-redbull/dhcp_scope_manager` |
| Templates synced from | `team-redbull/dhcp_scope_manager`, `helm/templates/` |

## How a values file becomes a DHCP scope

Add a `dhcp_values:` block to `sites/<site>/mces/<mce>/hostedClusters/<cluster>.yaml`
and the scope appears with the cluster. Nothing else to wire up.

```
day1 repo ──Argo polls ~3min──► renders this chart ──writes──► Request CR
                                                                   │
                              provider-http watches it ◄───────────┘
                                          │
                          ~1min: GET actual ──► DHCP API ──► Windows DHCP
                                 compare to payload.body
                                 differ → PUT · 404 → POST · CR deleted → DELETE
```

Two independent loops. **Argo CD** makes the cluster match git and never talks to
the DHCP server. **Crossplane** makes the DHCP server match the `Request` CR and
has never heard of git. The CR is the only thing they share: its
`spec.forProvider.payload.body` is the *desired* state, and the API's GET response
— stored back in `status.response` — is the *actual* state.

`oc describe request dhcp-scope-<network-dashed> -n crossplane-system` shows both sides.

## Values

The four values-repo files are layered on top of this chart's `values.yaml`, in
this order (last wins):

```
values.yaml (this chart)                              dhcp_api, crossplane
  → sites/configValues.yaml                           dhcp_values globals
    → sites/<site>/values.yaml
      → sites/<site>/mces/<mce>/values.yaml
        → sites/<site>/mces/<mce>/hostedClusters/<cluster>.yaml
```

The split is not "constant vs. varying" — it is **does the field end up inside the
request body sent to the DHCP API?**

- **`dhcp_values`** (including `failover`, `dnsServers`, `leaseDurationDays`) is
  desired scope state. It goes in the request body and is compared against what the
  DHCP server holds, so it is owned by the values repo, where CI validates it.
- **`dhcp_api`, `crossplane`** never appear in the body. They describe how to reach
  the API and which ProviderConfig to use, so they live here.

That boundary matters: `validate_dhcp_values.py` in `dhcp_scope_manager` walks the
values repo and **cannot see this file**. Anything moved here loses CI validation.

A cluster with no `dhcp_values` block renders nothing — the template is gated on
`dhcp_values.scopeName`.

## Templates are generated — do not edit

`templates/dhcp-scope-request.yaml` and `templates/_dhcp-helpers.tpl` are synced
from `team-redbull/dhcp_scope_manager`, which is their source of truth and holds
the test suite that renders them (including a parity test asserting the rendered
body equals what the API's GET returns).

```bash
# in a dhcp_scope_manager checkout
python3 scripts/sync_chart.py --target ../helm-charts-hostedclusters-setup
python3 scripts/sync_chart.py --target ../helm-charts-hostedclusters-setup --check   # CI
```

Editing them here is silently reverted on the next sync.

## Convergence

This repo follows the org's flat GitHub naming (`helm-charts-<name>`), the
GitHub-side spelling of GitLab's `helm-charts/<name>` subgroup. It exists so the
DHCP integration can be developed and tested against a real chart before it reaches
the air-gapped environment. Two ways it converges, undecided:

1. these two templates are merged into the GitLab chart, which stays canonical; or
2. the GitLab chart's `HostedCluster` / `NodePool` templates are added here and
   GitLab mirrors from GitHub.

Until then, treat the GitLab chart as the one that actually builds clusters.
