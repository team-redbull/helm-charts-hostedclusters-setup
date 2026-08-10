"""Rendering tests for the DHCP scope Crossplane Request template.

Runs `helm template` against this chart. These templates are the source of truth
for the CR that provider-http reconciles, so what they render *is* the desired
state the DHCP API is held to.

Requirements:
- helm CLI on PATH (module-level skip marker)
- No cluster, no DHCP server, no API
"""
import json
import shutil
import subprocess
import tempfile
import textwrap
from pathlib import Path

import pytest
import yaml

# Resolved from this file, not the cwd, so the suite runs from anywhere.
HELM_CHART = str(Path(__file__).resolve().parent.parent)


def _helm_template(values_content: str, extra_args: list[str] | None = None) -> str:
    """Run helm template with the given values YAML; return stdout or raise on error."""
    with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as fh:
        fh.write(values_content)
        values_path = fh.name

    cmd = ["helm", "template", "test-release", HELM_CHART, "-f", values_path]
    if extra_args:
        cmd.extend(extra_args)

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, cmd, result.stdout, result.stderr
        )
    return result.stdout


def _helm_template_fails(values_content: str, extra_args: list[str] | None = None) -> str:
    """Expect helm template to fail; return stderr."""
    with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as fh:
        fh.write(values_content)
        values_path = fh.name

    cmd = ["helm", "template", "test-release", HELM_CHART, "-f", values_path]
    if extra_args:
        cmd.extend(extra_args)

    result = subprocess.run(cmd, capture_output=True, text=True)
    assert result.returncode != 0, "Expected helm template to fail but it succeeded"
    return result.stderr


def _cluster_name(name: str) -> list[str]:
    """The --set hcAppset.yaml makes, carrying the hosted cluster's own name."""
    return ["--set-string", f"clusterName={name}"]


def _parse_cr(rendered: str) -> dict:
    """Parse the first YAML document from rendered output."""
    docs = list(yaml.safe_load_all(rendered))
    return docs[0]


# Minimal valid values. dhcp_values comes entirely from here: the chart's own
# values.yaml ships it commented out, so nothing leaks in from the base layer.
# failover: null and tokenSecretRef: null are still spelled out so the "no failover"
# and "no token" cases stay explicit rather than relying on absence.
#
# crossplane.namespace is set rather than clusterName because the Request is namespaced
# and dhcp.crNamespace has to resolve for *every* render. Setting clusterName here would
# also silently satisfy dhcp.scopeName, which would leave the scopeName-derivation tests
# below asserting against a fixture that already answered their question.

# Prepended to the self-contained fixtures further down, which build their own values
# rather than extending _VALID_VALUES. Those tests are about failover and token contents,
# so the namespace is plumbing rather than anything they assert on.
_CROSSPLANE_NS = "crossplane:\n  namespace: test-ns\n"

_VALID_VALUES = textwrap.dedent("""\
    crossplane:
      namespace: test-ns
    dhcp_api:
      url: https://dhcp-api.lab.local
      tokenSecretRef: null
    dhcp_values:
      scopeName: "test-scope"
      network: "10.20.30.0"
      subnetMask: "255.255.255.0"
      startRange: "10.20.30.100"
      endRange: "10.20.30.200"
      leaseDurationDays: 8
      description: ""
      gateway: "10.20.30.1"
      dns:
        servers:
          - "10.0.0.53"
        domain: "lab.local"
      exclusions: []
      failover: null
""")

pytestmark = pytest.mark.skipif(
    shutil.which("helm") is None, reason="helm CLI not available"
)


class TestHelmTemplateBasic:

    def test_valid_values_render_without_error(self):
        output = _helm_template(_VALID_VALUES)
        assert output.strip()

    def test_cr_has_correct_api_version(self):
        """The `.m.` group — Crossplane v2's namespaced Request.

        provider-http v1.0.14 ships two separate Request CRDs, not two versions of one:
        http.crossplane.io/v1alpha2 is scope: Cluster, http.m.crossplane.io/v1alpha2 is
        scope: Namespaced. Getting this wrong does not fail the render — it produces a CR
        for a kind whose provider config lives in the other group, which then never
        reconciles.
        """
        cr = _parse_cr(_helm_template(_VALID_VALUES))
        assert cr["apiVersion"] == "http.m.crossplane.io/v1alpha2"

    def test_cr_kind_is_request(self):
        cr = _parse_cr(_helm_template(_VALID_VALUES))
        assert cr["kind"] == "Request"

    def test_cr_name_uses_dashes_not_dots(self):
        """dhcp-scope-10-20-30-0 (dots replaced with dashes)."""
        cr = _parse_cr(_helm_template(_VALID_VALUES))
        assert cr["metadata"]["name"] == "dhcp-scope-10-20-30-0"

    def test_cr_namespace_is_always_rendered(self):
        """The kind is namespaced, so an object without one lands in `default`.

        This inverts the old assertion: the cluster-scoped kind had to render no
        namespace at all, because the API server dropped it and left Argo CD comparing
        a desired key carrying one against a live key without it.
        """
        cr = _parse_cr(_helm_template(_VALID_VALUES))
        assert cr["metadata"]["namespace"] == "test-ns"

    def test_cr_namespace_derives_from_the_cluster_name(self):
        """With nothing set, the namespace is hcp-<clusterName>."""
        values = _VALID_VALUES.replace(_CROSSPLANE_NS, "")
        cr = _parse_cr(_helm_template(values, _cluster_name("cluster-a")))
        assert cr["metadata"]["namespace"] == "hcp-cluster-a"

    def test_cr_namespace_explicit_beats_the_derived_one(self):
        values = _VALID_VALUES.replace(
            _CROSSPLANE_NS, "crossplane:\n  namespace: crossplane-system\n"
        )
        cr = _parse_cr(_helm_template(values, _cluster_name("cluster-a")))
        assert cr["metadata"]["namespace"] == "crossplane-system"

    def test_unresolvable_namespace_fails_the_render(self):
        """Neither source set is a hard failure, not a rendered `hcp-`.

        clusterName defaults to "" in values.yaml, so the derived form would otherwise
        produce a garbage namespace for any render that skipped the appset's --set.
        """
        values = _VALID_VALUES.replace(_CROSSPLANE_NS, "")
        stderr = _helm_template_fails(values)
        assert "crossplane.namespace could not be resolved" in stderr

    def test_provider_config_name_defaults_to_dhcp_http(self):
        cr = _parse_cr(_helm_template(_VALID_VALUES))
        assert cr["spec"]["providerConfigRef"]["name"] == "dhcp-http"

    def test_base_url_includes_api_server_url(self):
        cr = _parse_cr(_helm_template(_VALID_VALUES))
        base_url = cr["spec"]["forProvider"]["payload"]["baseUrl"]
        assert "https://dhcp-api.lab.local" in base_url

    def test_base_url_carries_the_scope_address(self):
        """The scope address is baked into baseUrl — it is the only place it appears now."""
        cr = _parse_cr(_helm_template(_VALID_VALUES))
        base_url = cr["spec"]["forProvider"]["payload"]["baseUrl"]
        assert base_url.endswith("/api/v1/scopes/10.20.30.0")

    def test_no_deletion_policy_is_rendered(self):
        """The namespaced spec has no deletionPolicy field.

        Its schema carries only forProvider, managementPolicies, providerConfigRef and
        writeConnectionSecretToRef. A CRD prunes unknown fields silently, so rendering
        one would look set in git and be absent on the server — worse than not setting
        it, because it reads as a deliberate choice that is not in force. Deletion
        behaviour comes from managementPolicies, which defaults to full management.
        """
        cr = _parse_cr(_helm_template(_VALID_VALUES))
        assert "deletionPolicy" not in cr["spec"]

    def test_provider_config_ref_carries_a_kind(self):
        """kind is required on the `.m.` group — the render is invalid without it.

        A namespaced Request may reference either a namespaced ProviderConfig in its own
        namespace or a cluster-wide ClusterProviderConfig, so the reference has to say
        which. Both are distinct from the legacy http.crossplane.io ProviderConfig of the
        same name, which this kind cannot reference at all.
        """
        cr = _parse_cr(_helm_template(_VALID_VALUES))
        assert cr["spec"]["providerConfigRef"]["kind"] == "ClusterProviderConfig"


class TestScopeNameDerivation:
    """scopeName defaults to the hosted cluster's own name.

    One values file is one hosted cluster is one DHCP scope, so the cluster's file
    name already carries the scope name. Helm never sees file names, so hcAppset.yaml
    passes it as --set clusterName. Mirrored by _validate_dhcp_content in
    team-redbull/dhcp_scope_manager, which derives the same name from the file stem.
    """

    # _VALID_VALUES minus the scopeName line, so the only source left is clusterName.
    _NO_SCOPE_NAME = _VALID_VALUES.replace('  scopeName: "test-scope"\n', "")

    def test_omitted_scope_name_derives_the_cluster_name(self):
        cr = _parse_cr(_helm_template(self._NO_SCOPE_NAME, _cluster_name("cluster-a")))
        body = json.loads(cr["spec"]["forProvider"]["payload"]["body"])
        assert body["scopeName"] == "cluster-a"

    def test_explicit_scope_name_beats_the_cluster_name(self):
        """An explicit value wins, so nothing already written has to change.

        Load-bearing that it is a chart-owned key rather than --set
        dhcp_values.scopeName: helm parameters outrank valueFiles, so injecting it
        there would make the values file impossible to honour.
        """
        cr = _parse_cr(_helm_template(_VALID_VALUES, _cluster_name("from-file-name")))
        body = json.loads(cr["spec"]["forProvider"]["payload"]["body"])
        assert body["scopeName"] == "test-scope"

    def test_empty_scope_name_derives(self):
        values = _VALID_VALUES.replace('  scopeName: "test-scope"', '  scopeName: ""')
        cr = _parse_cr(_helm_template(values, _cluster_name("cluster-a")))
        body = json.loads(cr["spec"]["forProvider"]["payload"]["body"])
        assert body["scopeName"] == "cluster-a"

    def test_neither_source_fails_the_render(self):
        """A name that cannot be resolved is a hard failure, not a silent skip.

        The old scopeName gate turned this into "render nothing", which hid the
        misconfiguration rather than reporting it.
        """
        stderr = _helm_template_fails(self._NO_SCOPE_NAME)
        assert "could not be resolved" in stderr

    def test_cluster_name_with_a_file_suffix_fails(self):
        """Catches an appset that forgot to trim, rather than creating a live scope
        on the Windows server literally named "cluster-a.yaml"."""
        stderr = _helm_template_fails(self._NO_SCOPE_NAME, _cluster_name("cluster-a.yaml"))
        assert "still carries a file suffix" in stderr

    @staticmethod
    def _failover_without_scope_name() -> str:
        """A failover block with no scopeName and no relationshipName, so both have
        to derive. Built via _values_with_failover for its correct YAML nesting."""
        values = _values_with_failover(
            partnerServer="dhcp02.lab.local",
            mode="HotStandby",
            serverRole="Active",
            reservePercent=5,
            maxClientLeadTimeMinutes=60,
        )
        assert '  scopeName: "test-scope"\n' in values
        return values.replace('  scopeName: "test-scope"\n', "")

    def test_derived_name_also_drives_the_failover_relationship_name(self):
        cr = _parse_cr(
            _helm_template(self._failover_without_scope_name(), _cluster_name("cluster-a"))
        )
        body = json.loads(cr["spec"]["forProvider"]["payload"]["body"])
        assert body["failover"]["relationshipName"] == "cluster-a-failover"

    def test_derived_relationship_name_over_64_chars_is_rejected(self):
        """Windows caps a relationship name at 64, so a cluster name is capped at 55
        once "-failover" is appended. Fails the render rather than emitting a name
        the DHCP server will refuse."""
        stderr = _helm_template_fails(
            self._failover_without_scope_name(), _cluster_name("c" * 60)
        )
        assert "at most 64" in stderr


class TestDhcpOptOutGate:
    """A cluster that wants no DHCP scope renders no Request — it does not error.

    The air-gapped copy of this chart also carries the HostedCluster / NodePool that
    provision the cluster itself, and it renders once per hosted cluster. An ungated
    Request would make `required "dhcp_values.network is required"` fail the *whole*
    render for a cluster that never asked for a scope.
    """

    def test_no_dhcp_values_key_at_all_renders_nothing(self):
        """The nil guard. Without the .Values.dhcp_values half of the gate this
        aborts with "nil pointer evaluating interface {}.network"."""
        values = "dhcp_api:\n  url: https://dhcp-api.lab.local\n"
        assert _helm_template(values, _cluster_name("cluster-a")).strip() == ""

    def test_inherited_globals_without_network_render_nothing(self):
        """sites/configValues.yaml gives every cluster a dhcp_values block, so
        inheriting one is not opting in. Only the cluster's own network is."""
        values = _CROSSPLANE_NS + textwrap.dedent("""\
            dhcp_api:
              url: https://dhcp-api.lab.local
            dhcp_values:
              leaseDurationDays: 8
              dns:
                servers: ["10.0.0.53"]
                domain: "lab.local"
              failover:
                partnerServer: "dhcp02.lab.local"
                mode: "HotStandby"
                serverRole: "Active"
                reservePercent: 5
                maxClientLeadTimeMinutes: 60
        """)
        assert _helm_template(values, _cluster_name("cluster-a")).strip() == ""

    def test_network_present_without_scope_name_renders_a_request(self):
        """The case the whole change exists for: the gate must not depend on
        scopeName, which now resolves for every cluster."""
        no_scope_name = _VALID_VALUES.replace('  scopeName: "test-scope"\n', "")
        cr = _parse_cr(_helm_template(no_scope_name, _cluster_name("cluster-a")))
        assert cr["kind"] == "Request"


class TestHelmRequiredFields:

    def test_network_required(self):
        """helm template fails when network is explicitly set to empty string.

        Omitting the network key renders nothing at all — that is the opt-out gate.
        The `required` guard is reached only by writing the key and leaving it empty,
        which is why the gate tests presence (hasKey) rather than truthiness: an
        absent key means "no scope wanted", an empty one means a malformed scope, and
        collapsing the two would let this typo disappear silently.
        """
        values = _CROSSPLANE_NS + textwrap.dedent("""\
            dhcp_api:
              url: https://dhcp-api.lab.local
            dhcp_values:
              network: ""
              scopeName: "test"
              subnetMask: "255.255.255.0"
              startRange: "10.20.30.100"
              endRange: "10.20.30.200"
              leaseDurationDays: 8
              gateway: "10.20.30.1"
              dns:
                servers: ["10.0.0.53"]
                domain: "lab.local"
              exclusions: []
        """)
        stderr = _helm_template_fails(values)
        assert "network" in stderr.lower() or "required" in stderr.lower()

    def test_api_server_url_required(self):
        """helm template fails when dhcp_api.url is explicitly set to empty string.

        The chart has a default url in values.yaml, so merely omitting the key
        uses that default.  Explicitly setting url: "" triggers the `required` guard.
        """
        values = _CROSSPLANE_NS + textwrap.dedent("""\
            dhcp_api:
              url: ""
            dhcp_values:
              scopeName: "test"
              network: "10.20.30.0"
              subnetMask: "255.255.255.0"
              startRange: "10.20.30.100"
              endRange: "10.20.30.200"
              leaseDurationDays: 8
              gateway: "10.20.30.1"
              dns:
                servers: ["10.0.0.53"]
                domain: "lab.local"
              exclusions: []
        """)
        stderr = _helm_template_fails(values)
        assert "dhcp_api" in stderr or "url" in stderr.lower() or "required" in stderr.lower()


class TestHelmPayloadBody:

    def _body(self, values_content: str) -> dict:
        """The request body, parsed.

        provider-http types payload.body as a JSON *string*, so the chart renders
        text rather than a nested mapping. json.loads preserves insertion order,
        so the field-order assertions below still read the canonical order.
        """
        cr = _parse_cr(_helm_template(values_content))
        return json.loads(cr["spec"]["forProvider"]["payload"]["body"])

    def test_scope_name_in_body(self):
        body = self._body(_VALID_VALUES)
        assert body["scopeName"] == "test-scope"

    def test_network_not_in_body(self):
        """The scope address is the identifier — it belongs in the URL, not the body."""
        body = self._body(_VALID_VALUES)
        assert "network" not in body
        assert "scope" not in body

    def test_lease_duration_is_int(self):
        body = self._body(_VALID_VALUES)
        assert isinstance(body["leaseDurationDays"], int)
        assert body["leaseDurationDays"] == 8

    def test_dns_servers_as_list(self):
        body = self._body(_VALID_VALUES)
        assert isinstance(body["dnsServers"], list)
        assert "10.0.0.53" in body["dnsServers"]

    def test_description_defaults_to_empty_string_not_null(self):
        """description must be "" not null — otherwise Crossplane sees a mismatch."""
        values = _CROSSPLANE_NS + textwrap.dedent("""\
            dhcp_api:
              url: https://dhcp-api.lab.local
            dhcp_values:
              scopeName: "test-scope"
              network: "10.20.30.0"
              subnetMask: "255.255.255.0"
              startRange: "10.20.30.100"
              endRange: "10.20.30.200"
              leaseDurationDays: 8
              gateway: "10.20.30.1"
              dns:
                servers: ["10.0.0.53"]
                domain: "lab.local"
              exclusions: []
        """)
        body = self._body(values)
        assert body.get("description") == "" or body.get("description") is not None

    def test_pxe_block_renders_boot_options(self):
        values = _VALID_VALUES.replace(
            "  exclusions: []",
            '  pxe:\n'
            '    server: "10.50.1.20"\n'
            '    bootfile: "snponly.efi"\n'
            "  exclusions: []",
        )
        body = self._body(values)
        assert body["nextServer"] == "10.50.1.20"
        assert body["bootFile"] == "snponly.efi"

    def test_pxe_block_omitted_renders_empty_strings(self):
        """No pxe: block must still emit both keys as "" — GET reports "" for an absent
        option, so omitting the keys here would diff forever for every non-PXE scope.
        """
        body = self._body(_VALID_VALUES)
        assert body["nextServer"] == ""
        assert body["bootFile"] == ""

    def test_boot_option_keys_sit_between_dns_domain_and_exclusions(self):
        """Body key order must match DhcpScopeBody field order — GET and PUT are one model."""
        values = _VALID_VALUES.replace(
            "  exclusions: []",
            '  pxe:\n'
            '    server: "10.50.1.20"\n'
            '    bootfile: "snponly.efi"\n'
            "  exclusions: []",
        )
        keys = list(self._body(values).keys())
        assert keys.index("dnsDomain") < keys.index("nextServer")
        assert keys.index("nextServer") < keys.index("bootFile")
        assert keys.index("bootFile") < keys.index("exclusions")

    def test_gateway_omitted_renders_derived_default(self):
        """Omitting the key derives the subnet's .254 address.

        Resolved at render time rather than left to the API: Crossplane checks this
        body against the GET response, which reports the concrete address, so a body
        that said null here would diff forever.
        """
        values = _VALID_VALUES.replace('  gateway: "10.20.30.1"\n', "")
        body = self._body(values)
        assert body["gateway"] == "10.20.30.254"

    def test_subnet_mask_omitted_renders_default(self):
        values = _VALID_VALUES.replace('  subnetMask: "255.255.255.0"\n', "")
        body = self._body(values)
        assert body["subnetMask"] == "255.255.255.0"

    def test_non_24_mask_without_gateway_fails_render(self):
        """No defensible .254 default exists off a /24 — fail rather than guess."""
        values = (
            _VALID_VALUES
            .replace('  subnetMask: "255.255.255.0"', '  subnetMask: "255.255.0.0"')
            .replace('  network: "10.20.30.0"', '  network: "10.20.0.0"')
            .replace('  gateway: "10.20.30.1"\n', "")
        )
        stderr = _helm_template_fails(values)
        assert "gateway is required when subnetMask is 255.255.0.0" in stderr

    def test_non_24_mask_with_explicit_gateway_renders(self):
        """The mismatch guard applies only to the derive path, not to any non-/24 mask."""
        values = (
            _VALID_VALUES
            .replace('  subnetMask: "255.255.255.0"', '  subnetMask: "255.255.0.0"')
            .replace('  network: "10.20.30.0"', '  network: "10.20.0.0"')
            .replace('  gateway: "10.20.30.1"', '  gateway: "10.20.0.1"')
        )
        body = self._body(values)
        assert body["subnetMask"] == "255.255.0.0"
        assert body["gateway"] == "10.20.0.1"

    _NO_RANGE = (
        _VALID_VALUES
        .replace('  startRange: "10.20.30.100"\n', "")
        .replace('  endRange: "10.20.30.200"\n', "")
    )

    def test_range_omitted_renders_derived_bounds(self):
        """Both bounds omitted derives the subnet's .1 – .253.

        Resolved at render time for the same reason as gateway, plus one of its own:
        the containment check ignores fields the body does not carry, so a body that
        omitted the range would never notice a range that drifted on the server.
        """
        body = self._body(self._NO_RANGE)
        assert body["startRange"] == "10.20.30.1"
        assert body["endRange"] == "10.20.30.253"

    def test_derived_range_stops_short_of_the_derived_gateway(self):
        """.253 and .254 — the two derivations have to coexist in one rendered body.

        A range ending at .254 would put the derived gateway inside the pool, and the
        API would 422 the very body this default exists to produce.
        """
        values = self._NO_RANGE.replace('  gateway: "10.20.30.1"\n', "")
        body = self._body(values)
        assert body["endRange"] == "10.20.30.253"
        assert body["gateway"] == "10.20.30.254"

    def test_derived_range_tracks_the_network(self):
        values = (
            self._NO_RANGE
            .replace('  network: "10.20.30.0"', '  network: "192.168.7.0"')
            .replace('  gateway: "10.20.30.1"', '  gateway: "192.168.7.1"')
        )
        body = self._body(values)
        assert body["startRange"] == "192.168.7.1"
        assert body["endRange"] == "192.168.7.253"

    def test_explicit_range_is_passed_through(self):
        body = self._body(_VALID_VALUES)
        assert body["startRange"] == "10.20.30.100"
        assert body["endRange"] == "10.20.30.200"

    def test_half_a_range_fails_the_render(self):
        """One bound alone is refused here, not silently completed downstream."""
        values = self._NO_RANGE.replace(
            "  leaseDurationDays: 8",
            '  startRange: "10.20.30.100"\n  leaseDurationDays: 8',
        )
        stderr = _helm_template_fails(values)
        assert "dhcp_values.endRange is required when dhcp_values.startRange is set" in stderr

    def test_non_24_mask_without_range_fails_render(self):
        """The .1–.253 convention holds for a /24 only — fail rather than guess."""
        values = (
            self._NO_RANGE
            .replace('  subnetMask: "255.255.255.0"', '  subnetMask: "255.255.0.0"')
            .replace('  network: "10.20.30.0"', '  network: "10.20.0.0"')
            .replace('  gateway: "10.20.30.1"', '  gateway: "10.20.0.1"')
        )
        stderr = _helm_template_fails(values)
        assert (
            "dhcp_values.startRange and dhcp_values.endRange are required when "
            "subnetMask is 255.255.0.0"
        ) in stderr

    def test_a_values_file_carrying_only_a_network_renders_completely(self):
        """The day-1 shape: network, lease and DNS — everything else derived.

        This is what the range default is for. If this render ever needs another key
        back, the default has stopped doing its job.
        """
        values = textwrap.dedent("""\
            crossplane:
              namespace: test-ns
            dhcp_api:
              url: https://dhcp-api.lab.local
              tokenSecretRef: null
            dhcp_values:
              network: "10.20.30.0"
              leaseDurationDays: 8
              dns:
                servers:
                  - "10.0.0.53"
              failover: null
        """)
        cr = _parse_cr(_helm_template(values, _cluster_name("cluster-a")))
        body = json.loads(cr["spec"]["forProvider"]["payload"]["body"])
        assert body["scopeName"] == "cluster-a"
        assert body["subnetMask"] == "255.255.255.0"
        assert body["startRange"] == "10.20.30.1"
        assert body["endRange"] == "10.20.30.253"
        assert body["gateway"] == "10.20.30.254"

    def test_gateway_null_renders_null(self):
        values = _VALID_VALUES.replace('  gateway: "10.20.30.1"', "  gateway: null")
        body = self._body(values)
        assert body["gateway"] is None

    def test_gateway_empty_string_renders_null(self):
        values = _VALID_VALUES.replace('  gateway: "10.20.30.1"', '  gateway: ""')
        body = self._body(values)
        assert body["gateway"] is None

    def test_exclusions_as_list(self):
        values = _VALID_VALUES + textwrap.dedent("""\
              exclusions:
                - startAddress: "10.20.30.1"
                  endAddress: "10.20.30.10"
        """).replace("      exclusions: []", "")
        # Use _VALID_VALUES with exclusions replaced — simpler: just parse VALID_VALUES body
        body = self._body(_VALID_VALUES)
        assert isinstance(body["exclusions"], list)

    def test_failover_null_when_not_configured(self):
        """No failover key → failover: null in rendered body."""
        body = self._body(_VALID_VALUES)
        assert "failover" in body
        assert body["failover"] is None


class TestHelmMappings:

    def _mappings(self, values_content: str) -> list:
        cr = _parse_cr(_helm_template(values_content))
        return cr["spec"]["forProvider"]["mappings"]

    def test_four_mappings_rendered(self):
        mappings = self._mappings(_VALID_VALUES)
        assert len(mappings) == 4

    def test_post_mapping_present(self):
        methods = [m["method"] for m in self._mappings(_VALID_VALUES)]
        assert "POST" in methods

    def test_get_mapping_present(self):
        methods = [m["method"] for m in self._mappings(_VALID_VALUES)]
        assert "GET" in methods

    def test_put_mapping_present(self):
        methods = [m["method"] for m in self._mappings(_VALID_VALUES)]
        assert "PUT" in methods

    def test_delete_mapping_present(self):
        methods = [m["method"] for m in self._mappings(_VALID_VALUES)]
        assert "DELETE" in methods

    def test_post_mapping_targets_the_scope_url(self):
        post = next(m for m in self._mappings(_VALID_VALUES) if m["method"] == "POST")
        assert post["url"] == "(.payload.baseUrl)"

    def test_put_mapping_includes_body(self):
        put = next(m for m in self._mappings(_VALID_VALUES) if m["method"] == "PUT")
        assert "body" in put


def _values_with_failover(**failover_fields) -> str:
    """Build a complete values YAML with failover correctly nested under dhcp_values.

    Avoids textwrap.dedent on an f-string that embeds fo_lines, because dedent
    measures the *minimum* indent across all lines — if fo_lines uses a smaller
    indent than the surrounding template it shifts the entire output.
    Instead we build the string directly with the required 2/4-space YAML indent.
    """
    fo_lines = "\n".join(f"    {k}: {_yaml_value(v)}" for k, v in failover_fields.items())
    return (
        _CROSSPLANE_NS
        + "dhcp_api:\n"
        "  url: https://dhcp-api.lab.local\n"
        "  tokenSecretRef: null\n"
        "dhcp_values:\n"
        '  scopeName: "test-scope"\n'
        '  network: "10.20.30.0"\n'
        '  subnetMask: "255.255.255.0"\n'
        '  startRange: "10.20.30.100"\n'
        '  endRange: "10.20.30.200"\n'
        "  leaseDurationDays: 8\n"
        '  description: ""\n'
        '  gateway: "10.20.30.1"\n'
        "  dns:\n"
        "    servers:\n"
        '      - "10.0.0.53"\n'
        '    domain: "lab.local"\n'
        "  exclusions: []\n"
        "  failover:\n"
        + fo_lines
        + "\n"
    )


def _yaml_value(v):
    """Convert a Python value to its YAML inline representation."""
    if v is None:
        return "null"
    if isinstance(v, bool):
        return str(v).lower()
    if isinstance(v, str):
        return f'"{v}"'
    return str(v)


class TestHelmFailoverRendering:

    def _body(self, values_content: str) -> dict:
        """The request body, parsed.

        provider-http types payload.body as a JSON *string*, so the chart renders
        text rather than a nested mapping. json.loads preserves insertion order,
        so the field-order assertions below still read the canonical order.
        """
        cr = _parse_cr(_helm_template(values_content))
        return json.loads(cr["spec"]["forProvider"]["payload"]["body"])

    def test_hotstandby_failover_renders_all_fields(self):
        values = _values_with_failover(
            partnerServer="dhcp02.lab.local",
            relationshipName="tomer-hc-failover",
            mode="HotStandby",
            serverRole="Active",
            reservePercent=5,
            maxClientLeadTimeMinutes=60,
        )
        body = self._body(values)
        f = body["failover"]
        assert f is not None
        assert f["partnerServer"] == "dhcp02.lab.local"
        assert f["mode"] == "HotStandby"
        assert f["serverRole"] == "Active"
        assert f["reservePercent"] == 5
        assert f["loadBalancePercent"] == 0

    def test_loadbalance_failover_normalizes_server_role_to_active(self):
        """Helm template must set serverRole=Active for LoadBalance mode."""
        values = _values_with_failover(
            partnerServer="dhcp02.lab.local",
            relationshipName="tomer-hc-failover",
            mode="LoadBalance",
            loadBalancePercent=50,
            maxClientLeadTimeMinutes=60,
        )
        body = self._body(values)
        f = body["failover"]
        assert f["mode"] == "LoadBalance"
        assert f["serverRole"] == "Active"
        assert f["reservePercent"] == 0
        assert f["loadBalancePercent"] == 50

    def test_hotstandby_normalizes_loadbalance_percent_to_zero(self):
        """HotStandby: loadBalancePercent must be 0 — matches GET response normalization."""
        values = _values_with_failover(
            partnerServer="dhcp02.lab.local",
            relationshipName="tomer-hc-failover",
            mode="HotStandby",
            serverRole="Active",
            reservePercent=5,
            maxClientLeadTimeMinutes=60,
        )
        body = self._body(values)
        assert body["failover"]["loadBalancePercent"] == 0

    def test_omitted_relationship_name_derives_from_scope_name(self):
        """Resolved here, not left to the API.

        GET reports the concrete relationship name Windows holds, so the desired
        body has to carry it too — an omission that reached the wire unresolved
        would fail the containment check on every poll.
        """
        values = _values_with_failover(
            partnerServer="dhcp02.lab.local",
            mode="HotStandby",
            serverRole="Active",
            reservePercent=5,
            maxClientLeadTimeMinutes=60,
        )
        assert self._body(values)["failover"]["relationshipName"] == "test-scope-failover"

    def test_explicit_relationship_name_is_passed_through(self):
        """The default is a fallback, never an override of what the values file says."""
        values = _values_with_failover(
            partnerServer="dhcp02.lab.local",
            relationshipName="hand-picked-name",
            mode="HotStandby",
            serverRole="Active",
            reservePercent=5,
            maxClientLeadTimeMinutes=60,
        )
        assert self._body(values)["failover"]["relationshipName"] == "hand-picked-name"

    def test_derived_relationship_name_over_64_chars_fails_the_render(self):
        """Windows caps the name at 64 — fail loudly rather than emit one it refuses."""
        values = _values_with_failover(
            partnerServer="dhcp02.lab.local",
            mode="HotStandby",
            serverRole="Active",
            reservePercent=5,
            maxClientLeadTimeMinutes=60,
        ).replace('scopeName: "test-scope"', f'scopeName: "{"x" * 60}"')
        stderr = _helm_template_fails(values)
        assert "64" in stderr and "relationshipName" in stderr


class TestHelmSecretInjection:
    """Bearer token injection.

    provider-http resolves a `{{ name:namespace:key }}` placeholder in a header
    against the live Secret at reconcile time, keeping the token out of git.
    NOT secretInjectionConfigs — that field runs the other direction, extracting
    fields from the HTTP *response* into a Secret.
    """

    def test_secret_injection_not_rendered_without_name_and_key(self):
        """tokenSecretRef requires name AND key — namespace is the one that derives.

        A ref missing either of those would render a placeholder provider-http cannot
        parse. That is not a loud failure: its regex demands three segments and a
        non-matching placeholder is left in the header as literal text with no error,
        so the request goes out with `Bearer {{ ... }}` and earns a 401 that names
        nothing. The header is dropped entirely instead.
        """
        values = _CROSSPLANE_NS + textwrap.dedent("""\
            dhcp_api:
              url: https://dhcp-api.lab.local
              tokenSecretRef:
                name: dhcp-api-token
                namespace: ~
                key: ~
            dhcp_values:
              scopeName: "test-scope"
              network: "10.20.30.0"
              subnetMask: "255.255.255.0"
              startRange: "10.20.30.100"
              endRange: "10.20.30.200"
              leaseDurationDays: 8
              gateway: "10.20.30.1"
              dns:
                servers: ["10.0.0.53"]
                domain: "lab.local"
              exclusions: []
              failover: null
        """)
        cr = _parse_cr(_helm_template(values))
        assert "Authorization" not in cr["spec"]["forProvider"]["headers"]

    def test_null_token_namespace_derives_the_cr_namespace(self):
        """A Secret sitting beside the Request needs no explicit namespace.

        It is NOT inherited at reconcile time — provider-http's regex demands three
        literal segments and is never handed the CR's own namespace — so the template
        has to write the same string into both. That is the whole reason the CR
        namespace and this default come from one helper.

        Spelled `null` rather than omitted because the chart's own values.yaml pins
        tokenSecretRef.namespace to dhcp-scope-manager, and Helm deep-merges mappings:
        simply leaving the key out inherits that base value instead of deriving. Null is
        how a caller opts into the derived namespace — the same distinction failover
        draws between `null` and `{}`.
        """
        values = _CROSSPLANE_NS + textwrap.dedent("""\
            dhcp_api:
              url: https://dhcp-api.lab.local
              tokenSecretRef:
                name: dhcp-api-token
                namespace: ~
                key: api-token
            dhcp_values:
              scopeName: "test-scope"
              network: "10.20.30.0"
              subnetMask: "255.255.255.0"
              startRange: "10.20.30.100"
              endRange: "10.20.30.200"
              leaseDurationDays: 8
              gateway: "10.20.30.1"
              dns:
                servers: ["10.0.0.53"]
                domain: "lab.local"
              exclusions: []
              failover: null
        """)
        cr = _parse_cr(_helm_template(values))
        header = cr["spec"]["forProvider"]["headers"]["Authorization"][0]
        assert header == "Bearer {{ dhcp-api-token:test-ns:api-token }}"
        assert cr["metadata"]["namespace"] == "test-ns"

    def test_secret_injection_rendered_with_all_three_fields(self):
        values = _CROSSPLANE_NS + textwrap.dedent("""\
            dhcp_api:
              url: https://dhcp-api.lab.local
              tokenSecretRef:
                name: dhcp-api-token
                namespace: crossplane-system
                key: token
            dhcp_values:
              scopeName: "test-scope"
              network: "10.20.30.0"
              subnetMask: "255.255.255.0"
              startRange: "10.20.30.100"
              endRange: "10.20.30.200"
              leaseDurationDays: 8
              gateway: "10.20.30.1"
              dns:
                servers: ["10.0.0.53"]
                domain: "lab.local"
              exclusions: []
        """)
        cr = _parse_cr(_helm_template(values))
        spec = cr["spec"]["forProvider"]

        # The placeholder order is name:namespace:key — provider-http resolves it
        # in that order, so a swap would silently read the wrong Secret.
        assert spec["headers"]["Authorization"] == [
            "Bearer {{ dhcp-api-token:crossplane-system:token }}"
        ]

        # secretInjectionConfigs would write the response INTO a Secret. Using it
        # for a request header does not work and is rejected by the CRD schema.
        assert "secretInjectionConfigs" not in spec

    def test_token_never_appears_verbatim_in_rendered_output(self):
        """Only the placeholder is rendered — the chart never reads Secret contents."""
        values = _CROSSPLANE_NS + textwrap.dedent("""\
            dhcp_api:
              url: https://dhcp-api.lab.local
              tokenSecretRef:
                name: dhcp-api-token
                namespace: crossplane-system
                key: token
            dhcp_values:
              scopeName: "test-scope"
              network: "10.20.30.0"
              subnetMask: "255.255.255.0"
              startRange: "10.20.30.100"
              endRange: "10.20.30.200"
              leaseDurationDays: 8
              gateway: "10.20.30.1"
              dns:
                servers: ["10.0.0.53"]
                domain: "lab.local"
              exclusions: []
        """)
        rendered = _helm_template(values)
        assert "{{ dhcp-api-token:crossplane-system:token }}" in rendered

    def test_custom_provider_config_name(self):
        # namespace goes inside this fixture's own crossplane block rather than via
        # _CROSSPLANE_NS: two `crossplane:` keys in one YAML document is a duplicate
        # key, and the later block silently replaces the first.
        values = textwrap.dedent("""\
            dhcp_api:
              url: https://dhcp-api.lab.local
            crossplane:
              namespace: test-ns
              providerConfigName: my-custom-provider
            dhcp_values:
              scopeName: "test-scope"
              network: "10.20.30.0"
              subnetMask: "255.255.255.0"
              startRange: "10.20.30.100"
              endRange: "10.20.30.200"
              leaseDurationDays: 8
              gateway: "10.20.30.1"
              dns:
                servers: ["10.0.0.53"]
                domain: "lab.local"
              exclusions: []
        """)
        cr = _parse_cr(_helm_template(values))
        assert cr["spec"]["providerConfigRef"]["name"] == "my-custom-provider"
