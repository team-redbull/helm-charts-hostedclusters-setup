"""Rendering tests for the hosted cluster: HostedCluster, NodePools, namespaces.

templates/hc.yaml and nodepool.yaml are ported from the air-gapped GitLab copy
of this chart. Three properties matter, and each fails quietly if broken:

1. They are OFF unless hostedCluster.enabled. Every cluster file in the values
   repo was written for a chart that rendered only DHCP, day2 and the
   release-segment hook; switched on by default, a slim file would suddenly
   render a HostedCluster nobody asked for.
2. With production values they render what the GitLab copy renders. The golden
   files in tests/golden/ ARE the GitLab copy's output (rendered from a sanitized
   export of it); every knob added here defaults to the GitLab behaviour.
3. A lab cluster (KubeVirt on the MCE, releases from quay.io) renders only fields
   HyperShift keeps as written. HyperShift saves the object through its Go types,
   which drop empty lists and empty optional fields; Argo CD then reports the
   HostedCluster OutOfSync for ever, and the upgrade workflow refuses to sync an
   Application with anything out of sync it did not expect (an empty
   imageContentSources was exactly such a field, and so was baseDomain: "",
   which HyperShift fills in for a KubeVirt cluster).

Requirements: helm CLI on PATH (module-level skip marker).
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

CHART = Path(__file__).resolve().parent.parent
FIXTURES = CHART / "tests" / "fixtures" / "hostedcluster"
GOLDEN = CHART / "tests" / "golden"

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm CLI not available")


class _NoDuplicatesLoader(yaml.SafeLoader):
    """SafeLoader that refuses a mapping key written twice (PyYAML keeps the last one silently)."""


def _no_duplicates(loader, node, deep=False):
    keys = [loader.construct_object(k, deep=deep) for k, _ in node.value]
    twice = sorted({k for k in keys if keys.count(k) > 1})
    if twice:
        raise yaml.constructor.ConstructorError(None, None, f"keys written twice: {twice}", node.start_mark)
    return loader.construct_mapping(node, deep=deep)


_NoDuplicatesLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicates)


def _helm(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["helm", "template", "t", str(CHART), *args], capture_output=True, text=True)


def _render(fixture: str | None = None, *extra: str, cluster: str = "tenant-a") -> list[dict]:
    args = ["--set-string", f"clusterName={cluster}"]
    if fixture:
        args += ["-f", str(FIXTURES / f"{fixture}.yaml")]
    r = _helm(*args, *extra)
    assert r.returncode == 0, r.stderr
    return [d for d in yaml.load_all(r.stdout, Loader=_NoDuplicatesLoader) if d]


def _kind(docs: list[dict], kind: str) -> list[dict]:
    return [d for d in docs if d["kind"] == kind]


def _hc(docs: list[dict]) -> dict:
    (hc,) = _kind(docs, "HostedCluster")
    return hc


def _by_key(docs: list[dict]) -> dict:
    return {(d["kind"], d["metadata"]["namespace"], d["metadata"]["name"]): d
            for d in docs if d["kind"] in ("HostedCluster", "NodePool")}


# --- 1. off unless asked for -------------------------------------------------------


def test_a_slim_cluster_file_renders_no_hosted_cluster():
    docs = _render(None, "--set-string", "dhcp_values.network=10.20.42.0", "--set-string", "mastertag=4.20.9-x86_64")
    kinds = {d["kind"] for d in docs}
    assert kinds.isdisjoint({"HostedCluster", "NodePool", "Namespace", "ConfigMap", "Policy"})
    assert "Request" in kinds  # the DHCP scope still renders, as before


def test_a_bare_render_still_renders():
    r = subprocess.run(["helm", "template", "t", str(CHART)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "HostedCluster" not in r.stdout


def test_enabled_without_a_cluster_name_fails_loudly():
    r = _helm("--set", "hostedCluster.enabled=true")
    assert r.returncode != 0
    assert "clusterName is required to render the hosted cluster" in r.stderr


# --- 2. the GitLab copy's output, for production values ------------------------------


@pytest.mark.parametrize("fixture", ["production-agent", "production-kubevirt-external"])
def test_production_values_render_what_the_gitlab_copy_renders(fixture):
    expected = [d for d in yaml.safe_load_all((GOLDEN / f"{fixture}.yaml").read_text()) if d]
    assert _by_key(_render(fixture)) == _by_key(expected)


def test_the_gitlab_copys_duplicate_tuning_config_key_is_gone():
    # The GitLab nodepool.yaml writes tuningConfig twice; YAML parsers keep the
    # second, which is the one rendered here. _render refuses duplicate keys.
    (nodepool, _) = _kind(_render("production-agent"), "NodePool")
    assert nodepool["spec"]["tuningConfig"] == [{"name": "tuned-tenant-a"}]


def test_production_references_its_secrets_and_ca_bundles_by_the_derived_names():
    spec = _hc(_render("production-agent"))["spec"]
    assert spec["pullSecret"] == {"name": "openshift-tenant-a-pull-secret"}
    assert spec["sshKey"] == {"name": "openshift-tenant-a-ssh-key"}
    assert spec["secretEncryption"]["aescbc"]["activeKey"] == {"name": "openshift-tenant-a-etcd-encryption-key"}
    assert spec["additionalTrustBundle"] == {"name": "user-ca-bundle"}
    assert spec["configuration"]["image"]["additionalTrustedCA"] == {"name": "custom-registries-config"}
    assert [p["type"] for p in spec["configuration"]["oauth"]["identityProviders"]] == ["LDAP", "HTPasswd"]


def test_the_namespaces_and_the_nodepool_configmaps():
    docs = _render("production-agent")
    assert {d["metadata"]["name"] for d in _kind(docs, "Namespace")} == {
        "hcp-tenant-a", "hcp-tenant-a-openshift-tenant-a",
    }
    assert {(d["metadata"]["namespace"], d["metadata"]["name"]) for d in _kind(docs, "ConfigMap")} == {
        ("hcp-tenant-a", "nm-conf-tenant-a"), ("hcp-tenant-a", "tuned-tenant-a"),
    }


def test_the_kyverno_policy_needs_its_own_switch():
    assert not _kind(_render("production-agent"), "Policy")
    (policy,) = _kind(_render("production-agent", "--set", "kyverno.enabled=true"), "Policy")
    assert policy["metadata"]["namespace"] == "hcp-tenant-a-openshift-tenant-a"


# --- 3. a lab cluster: KubeVirt on the MCE, releases from quay.io --------------------


def _lab(*extra: str) -> list[dict]:
    return _render("lab-kubevirt-local", *extra, cluster="upgrade-demo")


def test_lab_names_follow_the_production_derivation():
    docs = _lab()
    hc = _hc(docs)
    assert (hc["metadata"]["namespace"], hc["metadata"]["name"]) == ("hcp-upgrade-demo", "openshift-upgrade-demo")
    (nodepool,) = _kind(docs, "NodePool")
    assert nodepool["spec"]["clusterName"] == "openshift-upgrade-demo"
    assert hc["spec"]["pullSecret"] == {"name": "openshift-upgrade-demo-pull-secret"}


def test_lab_release_images_come_from_the_public_repository_without_a_cpo_image():
    docs = _lab()
    hc = _hc(docs)
    image = "quay.io/openshift-release-dev/ocp-release:4.19.53-x86_64"
    assert hc["spec"]["release"]["image"] == image
    assert "hypershift.openshift.io/control-plane-operator-image" not in hc["metadata"]["annotations"]
    assert [np["spec"]["release"]["image"] for np in _kind(docs, "NodePool")] == [image]


def test_lab_kubevirt_runs_on_the_mce_itself():
    hc = _hc(_lab())
    assert hc["spec"]["platform"] == {"kubevirt": {}, "type": "KubeVirt"}


def test_an_empty_base_domain_renders_no_dns():
    # HyperShift derives the domain and writes it into spec.dns.baseDomain; a
    # rendered "" would be OutOfSync for ever, and syncing it back is refused
    # ("baseDomain is immutable").
    assert "dns" not in _hc(_lab())["spec"]
    assert _hc(_render("production-agent"))["spec"]["dns"] == {"baseDomain": "example.internal"}


def test_lab_renders_none_of_the_production_references_it_has_nothing_for():
    spec = _hc(_lab())["spec"]
    for absent in ("sshKey", "secretEncryption", "additionalTrustBundle", "kubeAPIServerDNSName", "imageContentSources"):
        assert absent not in spec, absent
    assert set(spec["configuration"]) == {"scheduler", "operatorhub"}


def test_lab_networks_storage_and_availability_come_from_values():
    spec = _hc(_lab())["spec"]
    assert spec["networking"]["clusterNetwork"] == [{"cidr": "10.132.0.0/14"}]
    assert spec["networking"]["serviceNetwork"] == [{"cidr": "172.31.0.0/16"}]
    assert spec["etcd"]["managed"]["storage"]["persistentVolume"] == {"size": "8Gi", "storageClassName": "gp3-csi"}
    assert spec["controllerAvailabilityPolicy"] == spec["infrastructureAvailabilityPolicy"] == "SingleReplica"


def test_lab_services_are_what_hypershift_defaults_kubevirt_to():
    services = {s["service"]: s["servicePublishingStrategy"]["type"] for s in _hc(_lab())["spec"]["services"]}
    assert services == {"APIServer": "LoadBalancer", "OAuthServer": "Route", "Konnectivity": "Route", "Ignition": "Route"}


def test_lab_nodepool_is_an_in_place_vm_pool_on_the_pod_network():
    (nodepool,) = _kind(_lab(), "NodePool")
    spec = nodepool["spec"]
    assert spec["management"] == {"autoRepair": False, "upgradeType": "InPlace", "inPlace": {"maxUnavailable": 1}}
    assert spec["replicas"] == 2
    assert spec["platform"]["kubevirt"] == {
        "compute": {"cores": 2, "memory": "8Gi", "qosClass": "Burstable"},
        "rootVolume": {"persistent": {"size": "32Gi", "storageClass": "gp3-csi"}, "type": "Persistent"},
    }


def test_lab_renders_no_hook_and_no_dhcp_request():
    kinds = {d["kind"] for d in _lab()}
    assert kinds == {"Namespace", "HostedCluster", "NodePool"}


# --- the added knobs ---------------------------------------------------------------


def test_paused_until_pauses_the_hosted_cluster_and_every_nodepool():
    for value in ("true", "2026-11-01T00:00:00Z"):
        docs = _render("production-agent", "--set-string", f"pausedUntil={value}")
        assert _hc(docs)["spec"]["pausedUntil"] == value
        assert [np["spec"]["pausedUntil"] for np in _kind(docs, "NodePool")] == [value, value]


def test_paused_until_written_as_a_yaml_boolean_is_the_string_true():
    docs = _render("production-agent", "--set", "pausedUntil=true")
    assert _hc(docs)["spec"]["pausedUntil"] == "true"


def test_nothing_is_paused_by_default():
    docs = _render("production-agent")
    assert all("pausedUntil" not in d["spec"] for d in _kind(docs, "HostedCluster") + _kind(docs, "NodePool"))


def test_an_empty_image_content_source_list_is_not_rendered():
    # HyperShift drops an empty list when it saves the object, and Argo CD would
    # report the HostedCluster OutOfSync for ever.
    spec = _hc(_render("production-kubevirt-external", "--set", "imageContentSources=null"))["spec"]
    assert "imageContentSources" not in spec


def test_no_registries_and_no_ca_renders_no_image_block():
    spec = _hc(_render("production-kubevirt-external", "--set", "defaultImageRegistries=null",
                       "--set-string", "registriesTrustedCAName="))["spec"]
    assert "image" not in spec["configuration"]


def test_a_cpo_repo_renders_the_annotation_under_the_control_plane_image_path():
    hc = _hc(_lab("--set-string", "cpoRepo=ocp-release"))
    assert hc["metadata"]["annotations"]["hypershift.openshift.io/control-plane-operator-image"] == (
        "quay.io/openshift-release-dev/ocp-release:4.19.53-x86_64-hypershift"
    )


def test_references_can_be_renamed():
    spec = _hc(_render("production-agent", "--set-string", "pullSecretName=pull-secret",
                       "--set-string", "sshKeySecretName=lab-ssh-key"))["spec"]
    assert spec["pullSecret"] == {"name": "pull-secret"}
    assert spec["sshKey"] == {"name": "lab-ssh-key"}


def test_an_empty_pull_secret_name_fails():
    r = _helm("--set-string", "clusterName=x", "-f", str(FIXTURES / "lab-kubevirt-local.yaml"),
              "--set-string", "pullSecretName=")
    assert r.returncode != 0
    assert "pullSecretName cannot be empty" in r.stderr


def test_services_written_out_replace_the_built_in_branches():
    services = [{"service": "APIServer", "servicePublishingStrategy": {"type": "Route"}}]
    r = _helm("--set-string", "clusterName=x", "-f", str(FIXTURES / "lab-kubevirt-local.yaml"),
              "--set-json", f"services={json.dumps(services)}")
    assert r.returncode == 0, r.stderr
    hc = next(d for d in yaml.safe_load_all(r.stdout) if d and d["kind"] == "HostedCluster")
    assert hc["spec"]["services"] == services


def test_a_local_nodepool_may_still_join_an_extra_network():
    (nodepool,) = _kind(_lab("--set-string", "nodepool[0].additionalNetwork=default/nad-100",
                             "--set", "nodepool[0].attachDefaultNetwork=true"), "NodePool")
    kubevirt = nodepool["spec"]["platform"]["kubevirt"]
    assert kubevirt["additionalNetworks"] == [{"name": "default/nad-100"}]
    assert kubevirt["attachDefaultNetwork"] is True


def test_a_nodepools_annotations_reach_the_nodepool_and_its_labels_stay_the_three():
    # The workflows' update-nodepool reads redbull.io/scale-down-policy off the
    # NodePool, so it has to be writable in day1: as an annotation of the entry.
    (nodepool,) = _kind(_lab(
        "--set-json", 'nodepool[0].annotations={"redbull.io/scale-down-policy": "fewest-pods", "redbull.io/size": 3}',
        "--set-json", 'nodepool[0].labels={"allowDeletion": false, "team": "a"}',
    ), "NodePool")
    assert nodepool["metadata"]["annotations"] == {
        "argocd.argoproj.io/sync-wave": "4", "redbull.io/scale-down-policy": "fewest-pods", "redbull.io/size": "3",
    }
    assert nodepool["metadata"]["labels"] == {"allowDeletion": "false", "maxReplicas": "2", "minReplicas": "1"}


def test_a_nodepool_may_not_move_its_own_sync_wave():
    r = _helm("--set-string", "clusterName=x", "-f", str(FIXTURES / "lab-kubevirt-local.yaml"),
              "--set-json", 'nodepool[0].annotations={"argocd.argoproj.io/sync-wave": "1"}')
    assert r.returncode != 0
    assert "annotations may not set argocd.argoproj.io/sync-wave" in r.stderr


def test_an_unknown_kubevirt_infra_fails():
    r = _helm("--set-string", "clusterName=x", "-f", str(FIXTURES / "lab-kubevirt-local.yaml"),
              "--set-string", "kubevirtInfra=remote")
    assert r.returncode != 0
    assert 'kubevirtInfra "remote" is not one of external|local' in r.stderr
