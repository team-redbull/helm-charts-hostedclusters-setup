"""The rendered PUT body must equal what the DHCP API's GET returns.

This is the contract that keeps Crossplane from reconciling forever. provider-http
GETs the scope every ~60s and compares the response against the body rendered
here; any field that differs triggers a PUT, and a field that can never agree
triggers a PUT every 60 seconds for the lifetime of the cluster.

Half of that contract lives in team-redbull/dhcp_scope_manager, whose suite
asserts that GET returns CANONICAL_BODY. This file asserts that the template
renders CANONICAL_BODY. Neither repo imports the other: both pin the same
documented payload shape (CLAUDE.md §5 in the API repo), and a change to it has
to be made in both places deliberately — which is the point, because that shape
is the interface between them.

The duplication is the same trade the derived defaults already make: subnetMask
and gateway are resolved independently in Helm, in the API's Pydantic model and
in the values-repo CI validator, because each has to work without the others
present.
"""
import json
import shutil
import subprocess
import tempfile
import textwrap
from pathlib import Path

import pytest
import yaml

CHART = str(Path(__file__).resolve().parent.parent)

pytestmark = pytest.mark.skipif(
    shutil.which("helm") is None, reason="helm CLI not available"
)

# Exactly the payload shape the API documents and returns, field order included.
# Do not reorder: the API's response model fixes this order, and a values file
# that renders a different order renders a different JSON string.
CANONICAL_BODY = {
    "scopeName": "Cluster-A",
    "subnetMask": "255.255.255.0",
    "startRange": "10.20.30.100",
    "endRange": "10.20.30.200",
    "leaseDurationDays": 8,
    "description": "",
    "gateway": "10.20.30.254",
    "dnsServers": ["10.50.1.5", "10.50.1.6"],
    "dnsDomain": "lab.local",
    "nextServer": "",
    "bootFile": "",
    "exclusions": [
        {"startAddress": "10.20.30.1", "endAddress": "10.20.30.10"},
        {"startAddress": "10.20.30.241", "endAddress": "10.20.30.254"},
    ],
    "failover": None,
}

# subnetMask and gateway are deliberately absent — the chart derives them, and
# that derivation is what the first test below is really checking.
_VALUES = textwrap.dedent("""\
    dhcp_api:
      url: https://dhcp-api.lab.local
      tokenSecretRef: null
    dhcp_values:
      scopeName: "Cluster-A"
      network: "10.20.30.0"
      startRange: "10.20.30.100"
      endRange: "10.20.30.200"
      leaseDurationDays: 8
      dns:
        servers:
          - "10.50.1.5"
          - "10.50.1.6"
        domain: "lab.local"
      exclusions:
        - startAddress: "10.20.30.1"
          endAddress: "10.20.30.10"
        - startAddress: "10.20.30.241"
          endAddress: "10.20.30.254"
      failover: null
""")


def _rendered_body(values: str) -> dict:
    with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as fh:
        fh.write(values)
        path = fh.name
    result = subprocess.run(
        ["helm", "template", "parity", CHART, "-f", path],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    cr = next(iter(yaml.safe_load_all(result.stdout)))
    # payload.body is a JSON *string* — provider-http types the field as one, and
    # the API server rejects an object outright — so parse before comparing.
    return json.loads(cr["spec"]["forProvider"]["payload"]["body"])


def test_rendered_body_matches_the_api_payload_shape():
    """Rendered PUT body == the documented GET response, field for field."""
    assert _rendered_body(_VALUES) == CANONICAL_BODY


def test_rendered_field_order_matches_the_api():
    """Order is part of the contract, not cosmetic.

    The helper writes the body field by field rather than piping a map through
    toJson precisely because Go sorts map keys, which would silently reorder it.
    """
    assert list(_rendered_body(_VALUES).keys()) == list(CANONICAL_BODY.keys())


def test_omitted_gateway_derives_the_subnet_254():
    """A body carrying null against a server holding .254 never converges."""
    assert _rendered_body(_VALUES)["gateway"] == "10.20.30.254"


def test_empty_gateway_renders_null_and_stays_loop_free():
    """gateway: "" means no DHCP option 3, and a scope without option 3 reads
    back as null — so the pair still agrees."""
    body = _rendered_body(_VALUES + '  gateway: ""\n')
    assert body["gateway"] is None


def test_omitted_subnet_mask_derives_a_24():
    assert _rendered_body(_VALUES)["subnetMask"] == "255.255.255.0"


def test_exclusion_order_is_preserved_as_written():
    """The API returns exclusions sorted ascending by IP and provider-http compares
    lists by order, so a values file listing them out of order re-PUTs forever.
    The template must not reorder them — the values repo's CI validator is what
    enforces ascending order at the source."""
    body = _rendered_body(_VALUES)
    assert body["exclusions"] == CANONICAL_BODY["exclusions"]


def test_dns_server_order_is_preserved():
    """Primary/secondary semantics — never sorted, in either implementation."""
    assert _rendered_body(_VALUES)["dnsServers"] == ["10.50.1.5", "10.50.1.6"]
