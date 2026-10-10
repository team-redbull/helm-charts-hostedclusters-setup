"""Rendering tests for the release-segment PostDelete hook.

The hook starts the orchestrator's release-segment workflow once a hosted
cluster's Application and everything it managed are deleted. Three properties
are load-bearing, and each fails silently if broken:

1. It IS an Argo CD PostDelete hook. As a plain resource it would run on the
   first sync and release a live cluster's segment.
2. It renders without the cluster's values file. At PostDelete time that file is
   already deleted, so a hook gated on anything it sets (dhcp_values) would
   simply vanish and the segment would never be released.
3. The script treats 202 and 409 as success and everything else as a failure
   the Application is held for — checked against busybox wget's real output.

Requirements: helm CLI on PATH (module-level skip marker); `sh` for the script.
"""
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

CHART = str(Path(__file__).resolve().parent.parent)
SCRIPT = Path(CHART) / "files" / "release-segment.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("helm") is None, reason="helm CLI not available"
)

# Exactly the parameters hcAppset.yaml passes — and NO values file, which is the
# state the chart renders in at PostDelete time.
HCAPPSET = [
    "--set-string", "clusterName=ocp4-prep-gone-site1-a",
    "--set-string", "site=site1",
    "--set-string", "mce=ocp4-prep-mce-site1-a",
    "--set-string", "crossplane.namespace=dhcp-scope-manager",
]


def _render(*extra: str) -> list[dict]:
    r = subprocess.run(
        ["helm", "template", "t", CHART, *HCAPPSET, *extra],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    return [d for d in yaml.safe_load_all(r.stdout) if d]


def _hook(docs: list[dict]) -> dict | None:
    return next(
        (
            d for d in docs
            if d["kind"] == "Job"
            and d["metadata"]["labels"].get("app.kubernetes.io/component") == "segment-release"
        ),
        None,
    )


def _env(job: dict) -> dict[str, str]:
    return {e["name"]: e["value"] for e in job["spec"]["template"]["spec"]["containers"][0]["env"]}


def test_renders_from_the_appset_parameters_alone():
    job = _hook(_render())
    assert job is not None
    assert job["metadata"]["name"] == "release-segment-ocp4-prep-gone-site1-a"
    assert _env(job) == {
        "WORKFLOWS_API_URL": "http://workflows-orchestrator-api.redbull-workflows.svc:8080",
        "CLUSTER": "ocp4-prep-gone-site1-a",
        "SEGMENT_TYPE": "HC",
        "INSECURE_SKIP_TLS_VERIFY": "false",
    }


def test_is_a_post_delete_hook_and_never_a_synced_resource():
    annotations = _hook(_render())["metadata"]["annotations"]
    assert annotations["argocd.argoproj.io/hook"] == "PostDelete"
    # A failed Job stays for inspection and is replaced on Argo's retry.
    assert annotations["argocd.argoproj.io/hook-delete-policy"] == (
        "HookSucceeded,BeforeHookCreation"
    )


def test_an_empty_api_url_renders_no_hook():
    assert _hook(_render("--set-string", "release_segment.workflows_api_url=")) is None


def test_a_bare_render_without_a_cluster_name_renders_no_hook():
    r = subprocess.run(["helm", "template", "t", CHART], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert _hook([d for d in yaml.safe_load_all(r.stdout) if d]) is None


def test_the_tls_switch_and_type_are_passed_through():
    env = _env(_hook(_render(
        "--set", "release_segment.insecureSkipTLSVerify=true",
        "--set-string", "release_segment.type=MCE",
    )))
    assert env["INSECURE_SKIP_TLS_VERIFY"] == "true"
    assert env["SEGMENT_TYPE"] == "MCE"


def test_script_is_embedded_verbatim():
    args = _hook(_render())["spec"]["template"]["spec"]["containers"][0]["args"]
    assert args[0].rstrip("\n") == SCRIPT.read_text().rstrip("\n")


def test_pod_is_locked_down_like_the_day2_job():
    spec = _hook(_render())["spec"]["template"]["spec"]
    assert spec["automountServiceAccountToken"] is False
    assert spec["restartPolicy"] == "Never"
    container = spec["containers"][0]
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert container["image"] == "alpine/git:2.52.0"


# --- the script, against a fake busybox wget ---------------------------------
#
# The two output shapes are busybox wget's own: -S prints the status line on
# success; a 4xx/5xx makes it exit 1 with "server returned error: HTTP/1.1 ...".

_WGET_OK = """#!/bin/sh
echo '  HTTP/1.1 202 Accepted' >&2
echo '{"workflow_id":"release-segment-HC-c1","run_id":"r"}'
"""
_WGET_FAIL = """#!/bin/sh
echo '  HTTP/1.1 {code} X' >&2
echo 'wget: server returned error: HTTP/1.1 {code} X' >&2
exit 1
"""


def _run_script(tmp_path: Path, wget: str, cluster: str = "ocp4-prep-c1-site1-a"):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "wget"
    fake.write_text(wget)
    fake.chmod(0o755)
    (bin_dir / "sleep").write_text("#!/bin/sh\nexit 0\n")
    (bin_dir / "sleep").chmod(0o755)
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "WORKFLOWS_API_URL": "http://api.test:8080/",
        "CLUSTER": cluster,
        "SEGMENT_TYPE": "HC",
        "MAX_ATTEMPTS": "3",
    }
    return subprocess.run(["sh", str(SCRIPT)], env=env, capture_output=True, text=True)


def test_202_is_success(tmp_path):
    r = _run_script(tmp_path, _WGET_OK)
    assert r.returncode == 0, r.stderr
    assert "release-segment started" in r.stdout


def test_409_is_success(tmp_path):
    r = _run_script(tmp_path, _WGET_FAIL.format(code=409))
    assert r.returncode == 0, r.stderr
    assert "already in progress" in r.stdout


@pytest.mark.parametrize("code", [404, 422, 500, 503])
def test_anything_else_fails_after_the_retries(tmp_path, code):
    r = _run_script(tmp_path, _WGET_FAIL.format(code=code))
    assert r.returncode == 1
    assert r.stderr.count("attempt ") == 3
    assert "DeletionError" in r.stderr


def test_no_answer_at_all_fails(tmp_path):
    r = _run_script(tmp_path, "#!/bin/sh\necho 'wget: bad address' >&2\nexit 1\n")
    assert r.returncode == 1
    assert "answered nothing" in r.stderr


def test_a_cluster_name_that_would_break_the_json_is_refused(tmp_path):
    r = _run_script(tmp_path, _WGET_OK, cluster='c1","type":"MCE')
    assert r.returncode == 1
    assert "refusing cluster name" in r.stderr
