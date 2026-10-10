#!/bin/sh
# Ask the orchestrator to give this decommissioned cluster's segment back.
#
# Runs as an Argo CD PostDelete hook: Argo CD creates this Job only once every
# resource of the cluster's Application is deleted (the Crossplane Request, and
# in the air-gapped copy the HostedCluster and NodePool). That is the moment the
# release-segment workflow needs — before it, a Request that still exists would
# re-create the DHCP scope the run deletes.
#
# It STARTS the run and does not wait for it: the run is durable, answers on its
# own id (GET /workflows/runs/release-segment-<TYPE>-<cluster>), and may sit in
# a phase for a long time (a DHCP API outage holds the release back by design).
#
#   202  the run started                         -> success
#   409  a run for this cluster is already going -> success
#   anything else, or no answer                  -> retried HERE, with backoff
#        capped at 60s, until DEADLINE_SECONDS.
#
# The retrying has to happen here because Argo CD will not do it. Verified on
# OpenShift GitOps 1.18 (Argo CD 3.1): while this Job RUNS, the Application waits
# ("Waiting for N post-delete hooks to complete"), but a Job that FAILS counts as
# complete — Argo CD then finishes deleting the Application and never runs the
# hook again. So a failure here is final: the Failed Job is left behind as the
# record (HookSucceeded only cleans up a Job that succeeded), and its log ends
# with the exact request to send by hand.
#
# Inputs, all from the Job's env (see templates/release-segment-hook.yaml):
#   WORKFLOWS_API_URL          the orchestrator API base, no trailing path
#   CLUSTER                    this hosted cluster's name (hcAppset's clusterName)
#   SEGMENT_TYPE               the segment type to release (HC)
#   INSECURE_SKIP_TLS_VERIFY   "true" to skip TLS verification (ingress CA)
#   DEADLINE_SECONDS           optional, default 3300 (the Job's own
#                              activeDeadlineSeconds is 3600)
#   MAX_ATTEMPTS               optional cap on attempts, default 0 = none
#
# POSIX sh and busybox wget only: the image is alpine/git, already mirrored for
# the day2 registration Job, so this needs nothing new inside the air-gap.
set -eu

: "${WORKFLOWS_API_URL:?WORKFLOWS_API_URL is required}"
: "${CLUSTER:?CLUSTER is required}"
: "${SEGMENT_TYPE:?SEGMENT_TYPE is required}"
DEADLINE_SECONDS="${DEADLINE_SECONDS:-3300}"
MAX_ATTEMPTS="${MAX_ATTEMPTS:-0}"

# The name goes into a JSON body built with printf, so hold it to the shape an
# Argo CD Application name already has. Anything else is a bug upstream of
# here, and quoting it into JSON would hide it.
case "$CLUSTER" in
    "" | *[!a-z0-9.-]*)
        echo "refusing cluster name '$CLUSTER': expected lowercase letters, digits, '.' and '-'" >&2
        exit 1
        ;;
esac

URL="${WORKFLOWS_API_URL%/}/workflows/segment-lifecycle/release-segment"
BODY=$(printf '{"cluster":"%s","type":"%s"}' "$CLUSTER" "$SEGMENT_TYPE")

started=$(date +%s)
attempt=1
delay=5
while :; do
    # busybox wget exits non-zero on ANY 4xx/5xx, so the exit code cannot tell a
    # 409 (success here) from a 500. The status comes from the response line
    # instead: -S prints it on success, and the error message carries it on
    # failure — both as "HTTP/1.1 <code>", the last one being the answer.
    if [ "${INSECURE_SKIP_TLS_VERIFY:-false}" = "true" ]; then
        out=$(wget -S -O - -T 30 --no-check-certificate \
            --header 'Content-Type: application/json' --post-data "$BODY" "$URL" 2>&1) || true
    else
        out=$(wget -S -O - -T 30 \
            --header 'Content-Type: application/json' --post-data "$BODY" "$URL" 2>&1) || true
    fi
    status=$(printf '%s\n' "$out" | sed -n 's/.*HTTP\/[0-9.]* \([0-9][0-9][0-9]\).*/\1/p' | tail -n 1)

    case "$status" in
        202)
            id=$(printf '%s\n' "$out" | sed -n 's/.*"workflow_id": *"\([^"]*\)".*/\1/p' | head -n 1)
            echo "release-segment started for $CLUSTER ($SEGMENT_TYPE): ${id:-?} — status at GET /workflows/runs/${id:-<id>}"
            exit 0
            ;;
        409)
            echo "a release-segment run for $CLUSTER ($SEGMENT_TYPE) is already in progress"
            exit 0
            ;;
    esac

    echo "attempt $attempt: POST $URL answered ${status:-nothing}:" >&2
    printf '%s\n' "$out" | tail -n 3 >&2
    elapsed=$(( $(date +%s) - started ))
    if [ $((elapsed + delay)) -ge "$DEADLINE_SECONDS" ] \
        || { [ "$MAX_ATTEMPTS" -gt 0 ] && [ "$attempt" -ge "$MAX_ATTEMPTS" ]; }; then
        echo "giving up after ${elapsed}s and $attempt attempt(s). Argo CD does not retry a" >&2
        echo "failed PostDelete hook, so the segment is NOT released. Once the API answers, send:" >&2
        echo "  POST $URL $BODY" >&2
        exit 1
    fi
    sleep "$delay"
    attempt=$((attempt + 1))
    delay=$((delay * 2))
    [ "$delay" -gt 60 ] && delay=60
done
