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
| Payload contract | `CLAUDE.md` §5 in `team-redbull/dhcp_scope_manager` |

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

`oc describe request dhcp-scope-<network-dashed> -n <crossplane.namespace>` shows both
sides. The Request is the namespaced kind (`http.m.crossplane.io/v1alpha2`), so it lives
in the namespace `crossplane.namespace` resolves to — `hcp-<clusterName>` by default, not
`crossplane-system`. provider-http also ships a cluster-scoped `Request` in the legacy
`http.crossplane.io` group; `oc get request` may resolve to either, so spell out the group
(`oc get requests.http.m.crossplane.io -A`) when it matters.

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
`dhcp_values.scopeName`. Note the gate only works because nothing gives `scopeName` a
default: `dhcp_values` itself is truthy for every cluster once `sites/configValues.yaml`
sets the globals.

### Adding DNS servers per site

`dns.servers` is a **list**, and Helm replaces lists on merge rather than appending — a
site file that sets `dns.servers` wipes the entries from `sites/configValues.yaml`
instead of extending them. Use `dns.extraServers` for the per-site additions:

```yaml
# sites/configValues.yaml
dhcp_values:
  dns:
    servers: ["10.50.1.5", "10.50.1.6"]     # every site

# sites/<site>/values.yaml
dhcp_values:
  dns:
    extraServers: ["10.50.1.7"]             # this site only
```

renders `"dnsServers": ["10.50.1.5","10.50.1.6","10.50.1.7"]` — globals first, so they
keep the primary/secondary slots. Order is part of the comparison, not cosmetic.

Mappings need no such treatment: `failover` in a cluster file deep-merges onto the
global `partnerServer` / `mode` / `serverRole` as you would expect.

### Derived defaults

These fields may be omitted and are resolved to a concrete value **at render time**,
never left for the API to fill in — provider-http compares the GET response against
this body, and GET always reports concrete values. A field this body omits is not
compared at all, so leaving one to the API would also mean never detecting drift in it:

| Field | Derived as | Notes |
|---|---|---|
| `subnetMask` | `255.255.255.0` | |
| `gateway` | the subnet's `.254` | Only derivable for a /24; any other mask without an explicit gateway fails the render. `gateway: ""` means no option 3 and is honoured as written. |
| `startRange` + `endRange` | the subnet's `.1` – `.253` | Derived as a **pair** — one bound without the other fails the render. `.253` keeps the range clear of the derived `.254` gateway, which the API would otherwise reject as a gateway inside the leasable pool. /24 only, like `gateway`. Exclusions carve holes inside the range; they do not move its bounds. |
| `failover.relationshipName` | `<scopeName>-failover` | Windows caps it at 64 characters; a longer derived name fails the render rather than being truncated. |

## Templates and tests

`templates/dhcp-scope-request.yaml` and `templates/_dhcp-helpers.tpl` are **owned
here**. Edit them here; nothing overwrites them.

They used to be synced from `team-redbull/dhcp_scope_manager` with a
`GENERATED — DO NOT EDIT` header. That was backwards: the API repo does not deploy
this chart, and keeping a consumer's source in the producer's repo made the API
repo own something it never renders. The templates and their tests moved here
together, and `sync_chart.py` is gone.

```bash
pip install -r requirements.txt
pytest -q          # 53 tests: 45 rendering + 8 payload parity
helm lint .
```

Both test modules skip themselves without `helm` on PATH, so CI verifies the
binary is present before running them — otherwise a failed install turns the whole
suite into silent skips and the job still passes.

### What the tests cover

- **`tests/test_dhcp_scope_request.py`** — the rendered `Request` CR: object
  naming, `baseUrl`, all four verb mappings, the bearer-token placeholder, the
  `required` guards, and the gating on `dhcp_values.scopeName`.
- **`tests/test_render_parity.py`** — that `payload.body` equals the payload shape
  the DHCP API returns from `GET`, field order included.

That second one is the contract worth understanding. provider-http compares the
GET response against `payload.body` every ~60s and PUTs on any difference, so a
field this chart renders differently from what the API reports produces a PUT
every 60 seconds forever.

The check is deliberately **not** a cross-repo import. This repo asserts
`render == CANONICAL_BODY`; `dhcp_scope_manager` asserts `GET == the same shape`.
Both pin the payload documented in that repo's `CLAUDE.md` §5, so changing the
shape means changing it in both places on purpose — which is the point, since it
is the interface between them.

## Convergence

This repo follows the org's flat GitHub naming (`helm-charts-<name>`), the
GitHub-side spelling of GitLab's `helm-charts/<name>` subgroup. It exists so the
DHCP integration can be developed and tested against a real chart before it reaches
the air-gapped environment. Two ways it converges, undecided:

1. these two templates are merged into the GitLab chart, which stays canonical; or
2. the GitLab chart's `HostedCluster` / `NodePool` templates are added here and
   GitLab mirrors from GitHub.

Until then, treat the GitLab chart as the one that actually builds clusters.
