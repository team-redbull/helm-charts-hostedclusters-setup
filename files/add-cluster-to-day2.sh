#!/bin/sh
# Register this hosted cluster with each day2 sig repo by creating its folder.
#
# day2 has no registration file and no registry: a cluster exists for a sig when
# sites/<site>/<env>/mces/<mce>/<cluster>/ exists in that sig's repo. Creating
# that folder IS the onboarding (ARCHITECTURE.md R6). git cannot track an empty
# directory, so the folder needs one file in it; .gitkeep is the name day2's own
# docs mandate, and it never has to be removed — day2's discovery generators list
# directories only, so a stray file is invisible to them.
#
# Inputs, all from the Job's env (see add-cluster-to-day2-job.yaml):
#   SIG_TABLE      newline-delimited "name|repoURL|path"; path may be empty
#   BRANCH         branch to clone and push
#   SITE MCE DAY2_ENV CLUSTER
#   GIT_TOKEN      push credential, from a Secret, never on the command line
#   GIT_USER_NAME / GIT_USER_EMAIL
#   GIT_SSL_NO_VERIFY   "true" to skip TLS verification (internal CA)
#   WORKDIR        writable scratch; the container root filesystem is read-only
#
# Two names here are deliberate, and both avoid clobbering something the shell
# already owns: SIG_PATH (never PATH, which would break git itself) and DAY2_ENV
# (never ENV, which some shells treat as a startup file to source).
set -eu

: "${SIG_TABLE:?SIG_TABLE is required}"
: "${BRANCH:?BRANCH is required}"
: "${SITE:?SITE is required}"
: "${MCE:?MCE is required}"
: "${DAY2_ENV:?DAY2_ENV is required}"
: "${CLUSTER:?CLUSTER is required}"
: "${GIT_TOKEN:?GIT_TOKEN is required}"
WORKDIR="${WORKDIR:-/work}"
GIT_USER_NAME="${GIT_USER_NAME:-day2-cluster-registration}"
GIT_USER_EMAIL="${GIT_USER_EMAIL:-day2-cluster-registration@redbull.local}"
MAX_ATTEMPTS="${MAX_ATTEMPTS:-5}"

# Belt and braces. The token reaches git through a credential helper, so it never
# appears in a URL, in argv or in `git remote -v` and there should be nothing to
# redact — but git writes its own messages, and everything we echo from a git
# invocation goes through here so a future edit cannot quietly reintroduce a leak.
redact() {
    sed -e "s|${GIT_TOKEN}|***|g"
}

# The token reaches git only here, on the stdout of a helper git runs itself.
#
# Deliberately not embedded in the clone URL (https://user:token@host/...), which
# is what the day1 push automation does: a URL lands in error messages, in
# `git remote -v` and in the reflog, so that approach needs a redaction pass over
# all of them to stay safe. A helper has nothing to redact. It also drops the
# per-host username switch, since GitLab and GitHub both accept "oauth2".
git_c() {
    if [ "${GIT_SSL_NO_VERIFY:-false}" = "true" ]; then
        git -c credential.helper='!f(){ echo username=oauth2; echo "password=$GIT_TOKEN"; }; f' \
            -c http.sslVerify=false "$@"
    else
        git -c credential.helper='!f(){ echo username=oauth2; echo "password=$GIT_TOKEN"; }; f' \
            "$@"
    fi
}

# 0 if the cluster is registered with this sig (including "was already"), 1 if it
# genuinely failed.
register_one() {
    sig_name="$1"
    sig_url="$2"
    sig_path="$3"
    attempt="$4"

    checkout="$WORKDIR/$sig_name"
    rm -rf "$checkout"

    # Command substitution, not a pipe: `git ... | redact` would report sed's exit
    # status, which is always 0, so every failure would read as a success.
    if ! out=$(git_c clone --quiet --depth 1 --branch "$BRANCH" "$sig_url" "$checkout" 2>&1); then
        printf '%s\n' "$out" | redact >&2
        echo "  [$sig_name] clone failed" >&2
        return 1
    fi

    target="${sig_path:+$sig_path/}sites/$SITE/$DAY2_ENV/mces/$MCE/$CLUSTER"
    mce_dir="${sig_path:+$sig_path/}sites/$SITE/$DAY2_ENV/mces/$MCE"

    # THE idempotency gate, and the backfill path in one.
    #
    # This Job runs for every cluster the chart covers, not only new ones: the
    # commit that first ships this template makes every leaf Application
    # OutOfSync, so clusters day2 already knows about run it too. Their folders
    # exist, and that is a success, not a conflict — so stop here without
    # committing, without re-adding .gitkeep, and without touching a folder that
    # has since grown real chart folders of its own.
    if [ -d "$checkout/$target" ]; then
        echo "  [$sig_name] already present: $target"
        return 0
    fi

    # A brand-new MCE — or a whole site — may have no folder here yet; mkdir -p
    # creates every missing level, and committing the .gitkeep below makes all of
    # them real in git, which tracks files and infers directories. Worth
    # announcing: this registers a new MCE with this sig, a bigger structural
    # change than adding a cluster under an existing one.
    #
    # No .gitkeep at the MCE level: R7 wants one only for a folder that would
    # otherwise be empty, and this one now contains the cluster.
    if [ ! -d "$checkout/$mce_dir" ]; then
        echo "  [$sig_name] creating MCE folder $MCE (first cluster)"
    fi

    mkdir -p "$checkout/$target"
    : > "$checkout/$target/.gitkeep"

    if ! out=$(
        cd "$checkout" &&
        git config user.name "$GIT_USER_NAME" &&
        git config user.email "$GIT_USER_EMAIL" &&
        git add "$target/.gitkeep" &&
        git commit --quiet -m "chore: register $CLUSTER in day2 [day2-cluster-registration]" 2>&1
    ); then
        printf '%s\n' "$out" | redact >&2
        echo "  [$sig_name] commit failed" >&2
        return 1
    fi

    if out=$(git_c -C "$checkout" push --quiet origin "HEAD:$BRANCH" 2>&1); then
        echo "  [$sig_name] created $target"
        return 0
    fi

    # A rejected push means someone else pushed first — most likely a sibling Job,
    # when two clusters are added in one day1 commit. Throw the checkout away and
    # start over rather than merging: the whole operation is "create this folder if
    # it is missing", so a fresh clone re-runs the existence check against the new
    # tip and converges with no merge handling at all. Same convergence-by-re-clone
    # the day1 push automation uses.
    if [ "$attempt" -ge "$MAX_ATTEMPTS" ]; then
        printf '%s\n' "$out" | redact >&2
        echo "  [$sig_name] push failed after $MAX_ATTEMPTS attempts" >&2
        return 1
    fi
    echo "  [$sig_name] push rejected, re-cloning (attempt $((attempt + 1))/$MAX_ATTEMPTS)"
    sleep "$attempt"
    register_one "$sig_name" "$sig_url" "$sig_path" "$((attempt + 1))"
}

echo "Registering $CLUSTER (site=$SITE env=$DAY2_ENV mce=$MCE) with the day2 sig repos"

# A heredoc, not `echo "$SIG_TABLE" | while`: a piped loop runs in a subshell, so
# every assignment to $failed inside it would be discarded and the Job would exit 0
# no matter what went wrong.
failed=""
while IFS='|' read -r name url path; do
    [ -n "$name" ] || continue
    if ! register_one "$name" "$url" "$path" 1; then
        failed="$failed $name"
    fi
done <<SIGS
$SIG_TABLE
SIGS

if [ -n "$failed" ]; then
    echo "FAILED for:$failed" >&2
    exit 1
fi

echo "Done."
