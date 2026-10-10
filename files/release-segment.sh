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
#   anything else, or no answer                  -> retried here, then the Job
#        fails: Argo CD keeps the Application with a DeletionError condition and
#        retries the hook on its next reconcile. Visible, never silent.
#
# Inputs, all from the Job's env (see templates/release-segment-hook.yaml):
#   WORKFLOWS_API_URL          the orchestrator API base, no trailing path
#   CLUSTER                    this hosted cluster's name (hcAppset's clusterName)
#   SEGMENT_TYPE               the segment type to release (HC)
#   INSECURE_SKIP_TLS_VERIFY   "true" to skip TLS verification (ingress CA)
#   MAX_ATTEMPTS               optional, default 5
#
# POSIX sh and busybox wget only: the image is alpine/git, already mirrored for
# the day2 registration Job, so this needs nothing new inside the air-gap.
set -eu

: "${WORKFLOWS_API_URL:?WORKFLOWS_API_URL is required}"
: "${CLUSTER:?CLUSTER is required}"
: "${SEGMENT_TYPE:?SEGMENT_TYPE is required}"
MAX_ATTEMPTS="${MAX_ATTEMPTS:-5}"

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

    echo "attempt $attempt/$MAX_ATTEMPTS: POST $URL answered ${status:-nothing}:" >&2
    printf '%s\n' "$out" | tail -n 3 >&2
    if [ "$attempt" -ge "$MAX_ATTEMPTS" ]; then
        echo "giving up: the Application stays in DeletionError and Argo CD retries this hook" >&2
        exit 1
    fi
    sleep "$delay"
    attempt=$((attempt + 1))
    delay=$((delay * 2))
done
