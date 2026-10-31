{{/*
Helpers for the HostedCluster, its NodePools and the namespaces they live in
(hc.yaml, nodepool.yaml, namespace.yaml and the NodePool ConfigMaps).

Ported from the air-gapped GitLab copy of this chart
(redbull/helm-charts/hostedclusters-setup). Everything here renders only with
hostedCluster.enabled, so a cluster file that does not ask for it — every slim
file in the values repo today — renders exactly what it rendered before.
*/}}

{{/*
Is the hosted cluster rendered at all? "true" or "".
*/}}
{{- define "hc.enabled" -}}
{{- if (.Values.hostedCluster | default dict).enabled -}}true{{- end -}}
{{- end -}}

{{/*
The cluster's name, as hcAppset.yaml passes it: the file's own clusterName, else
the file name minus .yaml and the clusterFilePrefix (ocp4-). Everything else is
derived from it: HostedCluster openshift-<clusterName> in namespace
hcp-<clusterName>, HyperShift's control-plane namespace
hcp-<clusterName>-openshift-<clusterName>.
*/}}
{{- define "hc.clusterName" -}}
{{- required "clusterName is required to render the hosted cluster: hcAppset.yaml passes it as a parameter." .Values.clusterName -}}
{{- end -}}

{{/*
A release tag as a cluster file writes it (mastertag, workertag), completed:
a bare X.Y.Z (4.22.8) gets the -x86_64 arch suffix of the mirrored release
images; anything else (4.22.8-x86_64, another arch) is used as written.
*/}}
{{- define "hc.releaseTag" -}}
{{- $tag := toString (. | default "") -}}
{{- if regexMatch "^[0-9]+\\.[0-9]+\\.[0-9]+$" $tag -}}
{{- printf "%s-x86_64" $tag -}}
{{- else -}}
{{- $tag -}}
{{- end -}}
{{- end -}}

{{/*
The repository under imagePath (or imageHc) that holds the release images.
Production: <imagePath>/openshift/release-images, the one full mirror. A
connected lab takes the public one instead: imagePath
quay.io/openshift-release-dev, releaseRepo ocp-release. The upgrade rules'
release.repo (gitops-day1 upgrade/ladder.yaml) must describe the same layout,
because the upgrade workflow builds and pins the same references from it.
*/}}
{{- define "hc.releaseRepo" -}}
{{- .Values.releaseRepo | default "openshift/release-images" -}}
{{- end -}}

{{/*
A value the production chart hardcodes, made optional without changing what
production renders: the default while the key is absent, the value as written
once it is present — so an explicit "" switches the field off. hasKey, not
`default`, for the same reason nodepool.yaml uses it: an explicit empty value
must win.

    include "hc.orDefault" (list .Values "sshKeySecretName" "openshift-x-ssh-key")
*/}}
{{- define "hc.orDefault" -}}
{{- $values := index . 0 -}}
{{- $key := index . 1 -}}
{{- if hasKey $values $key -}}
{{- toString (index $values $key | default "") -}}
{{- else -}}
{{- index . 2 -}}
{{- end -}}
{{- end -}}

{{/*
Where a KubeVirt cluster's VMs run: "external" (the default, production's only
shape: another cluster, reached through the infra-kubeconfig Secret, VMs in
namespace openshift-<clusterName>) or "local" (the MCE itself, VMs in the
control-plane namespace). Emits "true" for local, "" otherwise.
*/}}
{{- define "hc.kubevirtLocal" -}}
{{- $infra := .Values.kubevirtInfra | default "external" -}}
{{- if not (has $infra (list "external" "local")) -}}
{{- fail (printf "kubevirtInfra %q is not one of external|local." $infra) -}}
{{- end -}}
{{- if eq $infra "local" -}}true{{- end -}}
{{- end -}}
