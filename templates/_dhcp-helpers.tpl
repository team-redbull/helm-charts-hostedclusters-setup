{{/*
Derive the default gateway — the subnet's .254 address — for a values file that omits
the key entirely. Resolved here rather than left to the API because Crossplane
checks the GET response against this rendered body: GET always reports the concrete
address the DHCP server holds, so the desired body must carry it too, or the check never
passes and Crossplane re-PUTs forever.

The .254 convention only holds for a /24, so any other mask without an explicit gateway
fails the render rather than guessing. Mirrors
DhcpScopePayload.resolve_default_gateway in app/models/scope.py.

Takes a dict with "network" and "mask". Emits a quoted IPv4 address.
*/}}
{{- define "dhcp.defaultGateway" -}}
{{- $mask := .mask -}}
{{- if ne $mask "255.255.255.0" -}}
{{- fail (printf "dhcp_values.gateway is required when subnetMask is %s: the default gateway is only derivable for 255.255.255.0. Set dhcp_values.gateway explicitly, or set it to \"\" for no gateway." $mask) -}}
{{- end -}}
{{- $octets := splitList "." (required "dhcp_values.network is required" .network) -}}
{{- if ne (len $octets) 4 -}}
{{- fail (printf "dhcp_values.network %q is not a valid IPv4 address" .network) -}}
{{- end -}}
{{- printf "%s.%s.%s.254" (index $octets 0) (index $octets 1) (index $octets 2) | quote -}}
{{- end -}}

{{/*
Resolve the DHCP distribution range.

Both bounds omitted derives the subnet's .1 – .253; both written are passed through; one
without the other is a hard failure. Mirrors DhcpScopePayload.resolve_default_range in
app/models/scope.py and _CiScopePayload.resolve_default_range in
scripts/validate_dhcp_values.py (team-redbull/dhcp_scope_manager) — change one, change all
three.

Resolved here rather than left to the API for the same reason as gateway: the containment
check in CLAUDE.md section 9 only compares fields the rendered body actually carries, so
omitting the range from the body would mean a range that drifted on the server was never
corrected. GET reports the concrete bounds Windows holds, so the body has to carry them.

.253 rather than .254 because .254 is the derived gateway (dhcp.defaultGateway). A range
ending on it would put the router inside the leasable pool, which the API rejects with a
422 — so the minimal values file, the one this default exists to enable, would never
render into anything valid.

The exclusion list is deliberately not consulted: exclusions carve holes inside a Windows
range, so .1–.253 minus the exclusions already *is* "every address that is not excluded",
and keeping the bounds fixed is what makes this reproducible in Go templates at all.

Takes a dict with "network", "mask", "start" and "end". Emits "<start> <end>", to be split
by the caller — a template can only return one string.
*/}}
{{- define "dhcp.distributionRange" -}}
{{- $start := .start | default "" -}}
{{- $end := .end | default "" -}}
{{- if and $start $end -}}
{{- printf "%s %s" $start $end -}}
{{- else if or $start $end -}}
{{- $present := ternary "startRange" "endRange" (ne $start "") -}}
{{- $missing := ternary "endRange" "startRange" (ne $start "") -}}
{{- fail (printf "dhcp_values.%s is required when dhcp_values.%s is set: the distribution range must be given as a pair, or left out entirely to derive .1-.253." $missing $present) -}}
{{- else -}}
{{- $mask := .mask -}}
{{- if ne $mask "255.255.255.0" -}}
{{- fail (printf "dhcp_values.startRange and dhcp_values.endRange are required when subnetMask is %s: the default range is only derivable for 255.255.255.0. Set both explicitly." $mask) -}}
{{- end -}}
{{- $octets := splitList "." (required "dhcp_values.network is required" .network) -}}
{{- if ne (len $octets) 4 -}}
{{- fail (printf "dhcp_values.network %q is not a valid IPv4 address" .network) -}}
{{- end -}}
{{- $prefix := printf "%s.%s.%s" (index $octets 0) (index $octets 1) (index $octets 2) -}}
{{- printf "%s.1 %s.253" $prefix $prefix -}}
{{- end -}}
{{- end -}}

{{/*
Resolve the scope name: dhcp_values.scopeName when a values file sets one, otherwise the
hosted cluster's own name.

One values file is one hosted cluster is one DHCP scope, so the file name already *is* the
name — repeating it in the file could only ever be redundant or wrong. It arrives as
.Values.clusterName because Helm has no notion of which file a value came from: it merges
the four valueFiles into one flat .Values. hcAppset.yaml passes it with --set, from the
same `.path.filename | trimSuffix` expression that names the Application (see values.yaml).

Resolved here rather than left to the API for the same reason as gateway: GET reports the
concrete name Windows holds, so the desired body must carry it too, or the containment
check in CLAUDE.md section 9 never passes and Crossplane re-PUTs forever. Mirrored by
_validate_dhcp_content in scripts/validate_dhcp_values.py (team-redbull/dhcp_scope_manager),
which derives the same name from the cluster file's stem — change one, change both.

An unresolvable name is a hard failure, not a silent skip: a skip is what the old
scopeName-based gate did, and it hid the misconfiguration instead of reporting it.
*/}}
{{- define "dhcp.scopeName" -}}
{{- $v := .Values.dhcp_values | default dict -}}
{{- $name := $v.scopeName | default .Values.clusterName -}}
{{- if not $name -}}
{{- fail "dhcp_values.scopeName could not be resolved: pass the hosted cluster's name as clusterName (hcAppset.yaml does this with --set), or set dhcp_values.scopeName explicitly." -}}
{{- end -}}
{{- /* hcAppset trims the suffix; catch a misconfigured one here rather than letting
       "cluster-a.yaml" become the literal scope name on the Windows server. */}}
{{- if or (hasSuffix ".yaml" $name) (hasSuffix ".yml" $name) -}}
{{- fail (printf "clusterName %q still carries a file suffix; hcAppset.yaml is meant to trim it. Left as-is this becomes the literal DHCP scope name on the Windows server." $name) -}}
{{- end -}}
{{- if gt (len $name) 256 -}}
{{- fail (printf "scopeName %q is %d characters; the API allows at most 256." $name (len $name)) -}}
{{- end -}}
{{- $name -}}
{{- end -}}

{{/*
The namespace the Request CR is created in.

Only meaningful for the namespaced Request kind (http.m.crossplane.io, Crossplane v2's
`.m.` API group). provider-http v1.0.14 ships *both* that and the legacy cluster-scoped
http.crossplane.io/v1alpha2 — namespaced is the newer of the two, not an older one.

Load-bearing twice over, which is why it resolves in one place: it is the CR's own
namespace, and it is the default namespace for the Bearer-token Secret placeholder.
provider-http parses `{{ name:namespace:key }}` with a regex requiring exactly three
segments (internal/data-patcher/parser.go) and never passes the CR's own namespace in,
so a placeholder cannot inherit it — the template has to write the same string into
both. Deriving them from one helper is what stops them disagreeing.

A placeholder that fails that regex is left in the header as literal text with no error
raised, so a wrong namespace here surfaces as a 401 from the DHCP API rather than as
anything naming the Secret.

Empty is a hard failure rather than a rendered `hcp-`: clusterName defaults to "" in
values.yaml, so the derived form silently produces a garbage namespace for any render
that reaches here without the appset's --set.
*/}}
{{- define "dhcp.crNamespace" -}}
{{- $ns := .Values.crossplane.namespace | default "" -}}
{{- if not $ns -}}
{{- if .Values.clusterName -}}
{{- $ns = printf "hcp-%s" .Values.clusterName -}}
{{- end -}}
{{- end -}}
{{- if not $ns -}}
{{- fail "crossplane.namespace could not be resolved: pass the hosted cluster's name as clusterName (hcAppset.yaml does this with --set) to derive hcp-<cluster>, or set crossplane.namespace explicitly." -}}
{{- end -}}
{{- $ns -}}
{{- end -}}

{{/*
The request body, as a JSON document.

Emitted as JSON text rather than as a YAML mapping because provider-http types
spec.forProvider.payload.body as a *string* — a nested mapping there is rejected
outright by the API server ("must be of type string"). The mappings then reach
into it with jq (.payload.body), which parses this text back into an object.

Written out field by field instead of piped through `toJson` so the field order
survives: Go marshals a map with its keys sorted, and this order is the canonical
one documented in CLAUDE.md section 5 and asserted in tests/test_helm.py.
*/}}
{{- define "dhcp.payload" -}}
{{- $v := .Values.dhcp_values | default dict -}}

{{- $dns := $v.dns | default dict -}}
{{- /* Globals first, then per-site additions. A site file cannot append to dns.servers:
       Helm deep-merges mappings but *replaces* lists, so `servers` in sites/<site>/values.yaml
       would silently wipe the ones from sites/configValues.yaml. extraServers is a separate
       key precisely so the merge keeps both halves.

       The concatenation order is the wire order and is load-bearing twice over: option 6 has
       primary/secondary semantics, and the containment check in CLAUDE.md section 9 compares
       the list element by element — reorder these and Crossplane re-PUTs on every poll. */}}
{{- $dnsServers := concat ($dns.servers | default (list)) ($dns.extraServers | default (list)) -}}
{{- $dnsDomain := $dns.domain | default "" -}}

{{- /* PXE options 66/67. Optional, but both-or-nothing — the API and the CI validator
       both reject half a pair, so the render is a straight pass-through. Absent keys
       become "", which is the concrete "no PXE options" state GET reports back. */}}
{{- $pxe := $v.pxe | default dict -}}
{{- $nextServer := $pxe.server | default "" -}}
{{- $bootFile := $pxe.bootfile | default "" -}}

{{- $mask := $v.subnetMask | default "255.255.255.0" -}}

{{- /* Both bounds omitted derives .1–.253; one alone fails the render. Split back apart
       because a template returns a single string. */}}
{{- $range := splitList " " (include "dhcp.distributionRange" (dict "network" $v.network "mask" $mask "start" $v.startRange "end" $v.endRange)) -}}

{{- /* Defaults to the hosted cluster's own name when no values file sets it. */}}
{{- $scopeName := include "dhcp.scopeName" . -}}

{{- $useFailover := and (hasKey $v "failover") $v.failover -}}

{
  "scopeName": {{ $scopeName | toJson }},
  "subnetMask": {{ $mask | toJson }},
  "startRange": {{ index $range 0 | toJson }},
  "endRange": {{ index $range 1 | toJson }},
  "leaseDurationDays": {{ $v.leaseDurationDays | int }},
  "description": {{ $v.description | default "" | toJson }},
  {{- /* Present-but-empty stays null (no DHCP option 3); only an absent key derives .254. */}}
  "gateway": {{ if hasKey $v "gateway" }}{{ $v.gateway | default nil | toJson }}{{ else }}{{ include "dhcp.defaultGateway" (dict "network" $v.network "mask" $mask) }}{{ end }},
  "dnsServers": {{ $dnsServers | toJson }},
  "dnsDomain": {{ $dnsDomain | toJson }},
  "nextServer": {{ $nextServer | toJson }},
  "bootFile": {{ $bootFile | toJson }},
  "exclusions": {{ $v.exclusions | default (list) | toJson }},
{{- if $useFailover }}
{{- $f := $v.failover }}
{{- /* relationshipName defaults to <scopeName>-failover. Resolved here rather than left to
       the API for the same reason as gateway: GET reports the concrete name Windows holds,
       so the desired body must carry it too or containment never passes. The API and the CI
       validator both keep the field required — they only ever see a resolved value.

       Windows caps a relationship name at 64 characters, so fail loudly rather than emit a
       name the DHCP server will refuse; _CiFailover enforces the same bound in CI. Note
       $scopeName may itself be derived from the cluster name, which caps that at 55. */}}
{{- $relationshipName := $f.relationshipName | default (printf "%s-failover" $scopeName) }}
{{- if gt (len $relationshipName) 64 }}
{{- fail (printf "failover.relationshipName %q is %d characters; Windows allows at most 64. The name derives from scopeName %q, which may itself come from the hosted cluster's name — shorten that, or set dhcp_values.failover.relationshipName explicitly." $relationshipName (len $relationshipName) $scopeName) }}
{{- end }}
  "failover": {
    "partnerServer": {{ $f.partnerServer | toJson }},
    "relationshipName": {{ $relationshipName | toJson }},
    "mode": {{ $f.mode | toJson }},
    "serverRole": {{ if eq $f.mode "LoadBalance" }}"Active"{{ else }}{{ $f.serverRole | toJson }}{{ end }},
    "reservePercent": {{ if eq $f.mode "LoadBalance" }}0{{ else }}{{ $f.reservePercent | default 0 | int }}{{ end }},
    "loadBalancePercent": {{ if eq $f.mode "HotStandby" }}0{{ else }}{{ $f.loadBalancePercent | int }}{{ end }},
    "maxClientLeadTimeMinutes": {{ $f.maxClientLeadTimeMinutes | int }}
  }
{{- else }}
  "failover": null
{{- end }}
}
{{- end }}
