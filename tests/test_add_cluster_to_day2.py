"""Rendering tests for the day2 cluster-registration Job.

The Job creates this hosted cluster's folder in each sig repo listed in
day2.copy_to_sigs, which is how day2 learns the cluster exists — there is no
registration file, only the folder (ARCHITECTURE.md R6 in gitops-day2-prod).

Two properties here are load-bearing and worth stating up front, because their
failure modes are both silent and both worse than a missing folder:

1. The Job carries NO Argo hook annotation. A hook re-runs on every sync; this
   must run once, which is exactly what an ordinary immutable Job gives.
2. The Job's name hashes its inputs. Unstable across identical renders and it
   re-runs forever; unchanged when the sig list changes and every such change
   becomes an unmergeable "field is immutable" sync failure.

Requirements: helm CLI on PATH (module-level skip marker). No cluster, no git.
"""
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
import yaml

CHART = str(Path(__file__).resolve().parent.parent)

# ---------------------------------------------------------------------------
# Environment shape — the ONLY things that differ between copies of this chart.
#
# This mirror points every sig at one GitHub repo with the tree under sigs/<sig>/.
# The air-gapped copy has one GitLab project per sig with the tree at the repo
# root, and a mirrored image. After editing templates/_day2-helpers.tpl and the
# Job's image line, update these three to match and the suite passes unchanged.
# ---------------------------------------------------------------------------
EXPECT_SIG_URL = "https://github.com/team-redbull/gitops-day2-prod.git"
EXPECT_SIG_PATH = "sigs/{sig}"          # "" in the air-gap: tree at the repo root
EXPECT_IMAGE = "alpine/git:2.52.0"


def _expect_line(sig: str) -> str:
    return f"{sig}|{EXPECT_SIG_URL.format(sig=sig)}|{EXPECT_SIG_PATH.format(sig=sig)}"

pytestmark = pytest.mark.skipif(
    shutil.which("helm") is None, reason="helm CLI not available"
)

# The --set flags hcAppset.yaml supplies. site and mce are passed as Helm
# parameters alongside clusterName; without them the env cannot be derived.
BASE = [
    "--set-string", "clusterName=ocp4-prod-herzi-site1",
    "--set-string", "site=site1",
    "--set-string", "mce=ocp4-prod-mce-site1-a",
    # The Request is namespaced and dhcp.crNamespace must resolve on every
    # render; set it here so these tests never depend on that template.
    "--set-string", "crossplane.namespace=dhcp-scope-manager",
]


def _render(*extra: str) -> str:
    cmd = ["helm", "template", "test-release", CHART, *BASE, *extra]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise subprocess.CalledProcessError(r.returncode, cmd, r.stdout, r.stderr)
    return r.stdout


def _render_values(values: str, *extra: str) -> str:
    """Render with a real values FILE.

    Not interchangeable with --set: `--set day2.copy_to_sigs={}` gives Helm a
    list holding one empty string, not an empty list, so the opt-out case can
    only be expressed the way a values file actually expresses it.
    """
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
        fh.write(values)
        path = fh.name
    return _render("-f", path, *extra)


def _render_fails(*extra: str) -> str:
    cmd = ["helm", "template", "test-release", CHART, *BASE, *extra]
    r = subprocess.run(cmd, capture_output=True, text=True)
    assert r.returncode != 0, f"expected failure, got:\n{r.stdout}"
    return r.stderr


def _job(rendered: str) -> dict | None:
    for doc in yaml.safe_load_all(rendered):
        if doc and doc.get("kind") == "Job":
            return doc
    return None


def _env(job: dict) -> dict[str, dict]:
    return {e["name"]: e for e in job["spec"]["template"]["spec"]["containers"][0]["env"]}


def _sigs(*names: str) -> list[str]:
    return ["--set", "day2.copy_to_sigs={%s}" % ",".join(names)]


# --------------------------------------------------------------------------
# The gate: no sigs, no Job
# --------------------------------------------------------------------------

def test_no_sigs_renders_no_job():
    """The default. Shipping this chart with copy_to_sigs empty must be inert —
    that is what makes the first rollout commit a pure no-op."""
    assert _job(_render()) is None


def test_empty_list_renders_no_job():
    """An explicit [] is how one site/MCE/cluster opts out, and because Helm
    replaces lists rather than merging them it is also how a narrower layer
    overrides a wider one. It must behave exactly like absent."""
    assert _job(_render_values("day2:\n  copy_to_sigs: []\n")) is None


def test_a_narrower_layer_replaces_the_wider_list():
    """Two values files, the way the four-layer stack in hcAppset supplies them.
    The second wins entirely — this is the documented way to narrow a scope."""
    wide = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    wide.write("day2:\n  copy_to_sigs: [redbull, nasa]\n"); wide.close()
    narrow = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    narrow.write("day2:\n  copy_to_sigs: [redbull]\n"); narrow.close()
    job = _job(_render("-f", wide.name, "-f", narrow.name))
    table = _env(job)["SIG_TABLE"]["value"]
    assert [l.split("|")[0] for l in table.strip().splitlines()] == ["redbull"]


def test_narrowing_the_list_does_not_wipe_the_charts_own_day2_keys():
    """The worry a shared `day2:` key invites. Helm deep-merges maps, so setting
    copy_to_sigs from the values repo must leave image/branch/token intact."""
    job = _job(_render_values("day2:\n  copy_to_sigs: [redbull]\n"))
    container = job["spec"]["template"]["spec"]["containers"][0]
    assert container["image"] == EXPECT_IMAGE
    assert _env(job)["BRANCH"]["value"] == "main"
    assert _env(job)["GIT_TOKEN"]["valueFrom"]["secretKeyRef"]["name"] == "day2-git-token"


# --------------------------------------------------------------------------
# The sig table
# --------------------------------------------------------------------------

def test_one_line_per_sig_built_from_the_name():
    table = _env(_job(_render(*_sigs("redbull", "nasa"))))["SIG_TABLE"]["value"]
    assert table.strip().splitlines() == [_expect_line("redbull"), _expect_line("nasa")]


@pytest.mark.parametrize("bad", ["../evil", "a/b", "RedBull", "-leading", ""])
def test_invalid_sig_name_fails_the_render(bad):
    """Names reach a clone URL and a path inside a repo this Job can push to, so
    a traversal here would write outside the folder it is allowed to touch."""
    err = _render_fails("--set", "day2.copy_to_sigs={%s}" % bad)
    assert "not a valid sig name" in err


# --------------------------------------------------------------------------
# Env derivation — day1 stores no env; it is derived from the MCE name
# --------------------------------------------------------------------------

@pytest.mark.parametrize("env", ["prod", "prep", "test"])
def test_env_derived_from_the_mce_name(env):
    job = _job(_render(
        "--set-string", f"mce=ocp4-{env}-mce-site1-a",
        "--set-string", f"clusterName=ocp4-{env}-herzi-site1",
        *_sigs("redbull"),
    ))
    assert _env(job)["DAY2_ENV"]["value"] == env


def test_unknown_env_fails_rather_than_creating_a_phantom_folder():
    """An env folder outside prod|prep|test fails day2's own lint, which breaks
    the render for the whole sig — not just this cluster."""
    err = _render_fails(
        "--set-string", "mce=ocp4-staging-mce-site1-a",
        "--set-string", "clusterName=ocp4-staging-herzi-site1",
        *_sigs("redbull"),
    )
    assert "not one of prod|prep|test" in err


def test_env_disagreement_between_mce_and_cluster_fails():
    """Both names carry the env. If they disagree one is wrong and the template
    cannot tell which, so it refuses rather than picking."""
    err = _render_fails(
        "--set-string", "mce=ocp4-prod-mce-site1-a",
        "--set-string", "clusterName=ocp4-prep-herzi-site1",
        *_sigs("redbull"),
    )
    assert "env mismatch" in err


def test_off_convention_mce_name_fails_with_a_pointer_to_the_override():
    err = _render_fails("--set-string", "mce=weird", *_sigs("redbull"))
    assert "cannot derive the day2 env" in err
    assert "day2.env" in err


def test_explicit_env_overrides_the_derivation():
    """The escape hatch for a single off-convention MCE."""
    job = _job(_render(
        "--set-string", "mce=totally-odd", "--set-string", "clusterName=odd-name",
        "--set-string", "day2.env=prep", *_sigs("redbull"),
    ))
    assert _env(job)["DAY2_ENV"]["value"] == "prep"


def test_explicit_env_is_still_validated():
    err = _render_fails("--set-string", "day2.env=staging", *_sigs("redbull"))
    assert "not one of prod|prep|test" in err


# --------------------------------------------------------------------------
# Runs once: no hook, and a name that changes only when the inputs do
# --------------------------------------------------------------------------

def test_no_argo_hook_annotation():
    """A hook annotation would silently restore per-sync re-running. Nothing
    else in this suite would notice, so it is pinned here."""
    ann = _job(_render(*_sigs("redbull")))["metadata"].get("annotations", {})
    assert not any("hook" in k for k in ann), ann
    assert ann["argocd.argoproj.io/sync-wave"] == "100"


def test_no_ttl_seconds_after_finished():
    """A TTL-deleted Job reads as missing to Argo, so selfHeal recreates it and
    it runs again — a loop."""
    assert "ttlSecondsAfterFinished" not in _job(_render(*_sigs("redbull")))["spec"]


def test_job_name_is_stable_across_identical_renders():
    a = _job(_render(*_sigs("redbull")))["metadata"]["name"]
    b = _job(_render(*_sigs("redbull")))["metadata"]["name"]
    assert a == b


def test_job_name_changes_when_a_sig_is_added():
    """Without this, adding a sig tries to patch an immutable Job spec and the
    app's sync fails permanently."""
    one = _job(_render(*_sigs("redbull")))["metadata"]["name"]
    two = _job(_render(*_sigs("redbull", "nasa")))["metadata"]["name"]
    assert one != two


def test_job_names_differ_between_clusters():
    a = _job(_render(*_sigs("redbull")))["metadata"]["name"]
    b = _job(_render("--set-string", "clusterName=ocp4-prod-karniol-site1",
                     *_sigs("redbull")))["metadata"]["name"]
    assert a != b


def test_job_name_fits_kubernetes_limits_even_for_a_long_cluster_name():
    """Job names cap at 63, and the pods a Job creates append a suffix."""
    name = _job(_render(
        "--set-string", "clusterName=ocp4-prod-an-extremely-long-cluster-name-site1",
        *_sigs("redbull"),
    ))["metadata"]["name"]
    assert len(name) <= 57, name
    assert name.strip("-") == name


# --------------------------------------------------------------------------
# The container
# --------------------------------------------------------------------------

def test_image_is_pinned_and_hardcoded():
    """A literal in the template, edited by hand per environment — not a value, so
    nothing downstream can swap the image a token-bearing pod runs. Never :latest:
    a floating tag would mean different things on the two sides of the air-gap."""
    container = _job(_render(*_sigs("redbull")))["spec"]["template"]["spec"]["containers"][0]
    assert container["image"] == EXPECT_IMAGE
    assert ":latest" not in container["image"]


def test_token_comes_from_a_secret_and_never_from_argv():
    job = _job(_render(*_sigs("redbull")))
    container = job["spec"]["template"]["spec"]["containers"][0]
    token = _env(job)["GIT_TOKEN"]
    assert token["valueFrom"]["secretKeyRef"] == {"name": "day2-git-token", "key": "token"}
    assert "value" not in token
    assert "GIT_TOKEN" not in " ".join(container.get("command", []))


def test_runs_unprivileged_with_a_writable_scratch_dir():
    """OpenShift's restricted SCC, plus the emptyDir git needs given a read-only
    root filesystem — HOME must point at it or git cannot write its config."""
    spec = _job(_render(*_sigs("redbull")))["spec"]["template"]["spec"]
    container = spec["containers"][0]
    assert spec["securityContext"]["runAsNonRoot"] is True
    assert spec["automountServiceAccountToken"] is False
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert container["securityContext"]["capabilities"]["drop"] == ["ALL"]
    assert spec["volumes"][0]["emptyDir"] == {}
    env = _env(container if False else _job(_render(*_sigs("redbull"))))
    assert env["HOME"]["value"] == "/work"
    assert env["WORKDIR"]["value"] == "/work"


def test_script_is_embedded_verbatim():
    """The script is a real file so it can be shellchecked and unit-tested; the
    template only inlines it. A YAML mangling here would be invisible until it
    ran against a live sig repo."""
    args = _job(_render(*_sigs("redbull")))["spec"]["template"]["spec"]["containers"][0]["args"]
    on_disk = (Path(CHART) / "files" / "add-cluster-to-day2.sh").read_text()
    assert args[0].rstrip("\n") == on_disk.rstrip("\n")


def test_cluster_without_dhcp_values_still_registers():
    """The Request is gated on dhcp_values.network. This Job is not: a cluster
    that wants no DHCP scope still belongs in day2."""
    rendered = _render(*_sigs("redbull"))
    kinds = [d["kind"] for d in yaml.safe_load_all(rendered) if d]
    assert kinds == ["Job"]
