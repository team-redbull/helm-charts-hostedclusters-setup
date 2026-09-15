{{/*
Resolve the day2 environment segment for this cluster.

day2's tree has an <env> level that day1's does not:

    day1:  sites/<site>/mces/<mce>/hostedClusters/<cluster>.yaml
    day2:  sites/<site>/<env>/mces/<mce>/<cluster>/

Nothing in the values repo stores the environment. It exists only inside the
names, by the convention ocp4-<env>-<name>-<site>[-<seq>] — field 2. day2's own
ARCHITECTURE.md already relies on this in the other direction ("translating a
day2 path into a day1 path means dropping the <env> segment"), so deriving it
here is the same assumption day2 already makes, not a new one.

Derived rather than declared on purpose. A hand-maintained env key would be a
second place the same fact lives, free to drift from the name that every other
consumer reads — the failure mode version.yaml's own header warns about. A
derivation cannot drift; it can only fail loudly, which is what the guards below
are for.

Both names are checked because they must agree: day2 requires that "the env and
site in the name must agree with the folder's position", and an env folder
outside prod|prep|test fails day2's own lint_sigs_tree(). Getting this wrong
does not produce a broken cluster — it produces a phantom folder that breaks the
*whole sig's* render, for every team. Hence fail, never guess.

An explicit day2.env short-circuits all of it, so one off-convention MCE is a
one-line fix in its values.yaml rather than a blocked rollout.
*/}}
{{- define "day2.env" -}}
{{- $envs := list "prod" "prep" "test" -}}
{{- $explicit := (.Values.day2 | default dict).env | default "" -}}
{{- if $explicit -}}
{{- if not (has $explicit $envs) -}}
{{- fail (printf "day2.env %q is not one of prod|prep|test. day2's render check (lint_sigs_tree) rejects any other env folder, so this would break the sig's render rather than just this cluster." $explicit) -}}
{{- end -}}
{{- $explicit -}}
{{- else -}}
{{- $mce := required "mce is required to derive the day2 env: hcAppset.yaml passes it with --set." .Values.mce -}}
{{- $parts := splitList "-" $mce -}}
{{- if lt (len $parts) 2 -}}
{{- fail (printf "cannot derive the day2 env from mce %q: expected the convention ocp4-<env>-<name>-<site>. Set day2.env explicitly in this MCE's values.yaml if it is deliberately off-convention." $mce) -}}
{{- end -}}
{{- $env := index $parts 1 -}}
{{- if not (has $env $envs) -}}
{{- fail (printf "derived day2 env %q from mce %q, which is not one of prod|prep|test. day2's render check rejects any other env folder, so this would break the sig's render rather than just this cluster. Set day2.env explicitly if this MCE is deliberately off-convention." $env $mce) -}}
{{- end -}}
{{- /* The cluster's own name carries the env too. If the two disagree, one of them
       is wrong and we cannot tell which — so refuse rather than pick. */}}
{{- $cluster := required "clusterName is required to derive the day2 env: hcAppset.yaml passes it with --set." .Values.clusterName -}}
{{- $cparts := splitList "-" $cluster -}}
{{- if lt (len $cparts) 2 -}}
{{- fail (printf "cannot check the day2 env against clusterName %q: expected the convention ocp4-<env>-<name>-<site>." $cluster) -}}
{{- end -}}
{{- $cenv := index $cparts 1 -}}
{{- if ne $cenv $env -}}
{{- fail (printf "env mismatch: mce %q says %q but clusterName %q says %q. day2 requires the env in a name to agree with the folder's position, and one of these two names is wrong — fix the name rather than setting day2.env to paper over it." $mce $env $cluster $cenv) -}}
{{- end -}}
{{- $env -}}
{{- end -}}
{{- end -}}

{{/*
Validate one sig name and emit it.

Sig names come from the values repo and flow straight into a clone URL and a
filesystem path inside the checkout. A name carrying "/" or ".." would write
outside the folder this Job is meant to touch, in a repo it has push rights to,
so this is a security boundary and not only a typo guard.
*/}}
{{- define "day2.validSigName" -}}
{{- $sig := . -}}
{{- if not (regexMatch "^[a-z0-9][a-z0-9-]*$" $sig) -}}
{{- fail (printf "day2.copy_to_sigs entry %q is not a valid sig name: expected lowercase letters, digits and dashes, starting with a letter or digit. The name becomes part of a repo URL and a path inside the checkout." $sig) -}}
{{- end -}}
{{- $sig -}}
{{- end -}}

{{/*
The sig table the script reads: one "name|repoURL|path" line per sig.

Shell-parseable rather than JSON because the image is a plain git image with no
jq and no python — see values.yaml. The script splits on "|" with `IFS`.

repoURL and path are built from the name alone; every sig repo has the same
shape, so there is nothing per-sig to configure. What these two lines become in
the air-gapped GitLab is recorded in APPLY-DAY2-COPY.md (gitops-day1/argocd-platform)
rather than as a comment here — one document carries the whole mirror delta.
*/}}
{{- define "day2.sigTable" -}}
{{- range $sig := .Values.day2.copy_to_sigs -}}
{{- $name := include "day2.validSigName" $sig -}}
{{- $url := "https://github.com/team-redbull/gitops-day2-prod.git" -}}
{{- $path := printf "sigs/%s" $name -}}
{{- printf "%s|%s|%s\n" $name $url $path -}}
{{- end -}}
{{- end -}}

{{/*
The Job's name, ending in a hash of everything the Job acts on.

This is load-bearing, and its failure mode is worse than a missing folder. The
Job is an ordinary resource, not a hook, which is what makes it run exactly once
— Argo applies it and every later sync is a no-op against the completed object.
But a Job's spec is immutable: if the rendered spec ever changes under a name
that already exists, the API server rejects the apply with "field is immutable"
and the app's sync fails, and keeps failing, until someone deletes the Job by
hand.

Hashing the inputs into the name means a changed input produces a *different*
Job rather than an illegal patch of the existing one. So:

  - nothing changed  -> same name -> Argo no-ops -> the Job does not re-run
  - a sig was added  -> new name  -> a new Job runs and registers the new sig

which is exactly the behaviour wanted at both ends.

The hash covers the sig table (so adding, removing or renaming a sig re-runs),
and site/mce/env/cluster (so a cluster that somehow moved re-registers). It does
NOT cover the image or the token — changing those should not silently re-run
every Job in the fleet; deleting the Job is the deliberate way to retry.

The cluster name is truncated to keep the whole thing inside the 63-character
limit a Job name has, with room for the "-<5 random>" suffix its pods get.
Truncation cannot cause a collision: the hash is taken over the *full* name.
*/}}
{{- define "day2.jobName" -}}
{{- $env := include "day2.env" . -}}
{{- $input := printf "%s|%s|%s|%s|%s" (include "day2.sigTable" .) .Values.site .Values.mce $env .Values.clusterName -}}
{{- $hash := sha256sum $input | trunc 8 -}}
{{- $stem := .Values.clusterName | trunc 26 | trimSuffix "-" -}}
{{- printf "add-cluster-to-day2-%s-%s" $stem $hash -}}
{{- end -}}
