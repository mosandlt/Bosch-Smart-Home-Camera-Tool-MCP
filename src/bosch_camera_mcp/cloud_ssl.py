"""TLS trust for Bosch public cloud endpoints — CWE-295 / GHSA-6qh5-x5m5-vj6v fix.

Bosch's residential cloud API (``residential.cbs.boschsecurity.com``) and the
live video proxy (``proxy-*.cbs.boschsecurity.com``) are served by a *private*
Bosch PKI (``Bosch ST Root CA`` -> ``Video CA 2A``) absent from any public trust
store.  Historically every outbound call to those hosts used ``verify=False``
(CWE-295), accepting any certificate and allowing an adjacent-network attacker
to MITM OAuth tokens and cloud traffic.

This module builds a single :class:`ssl.SSLContext` that trusts BOTH the system
roots (for the Let's Encrypt OAuth host and any other public host) AND the Bosch
private CA (for the cloud REST API and the video proxy). It rejects self-signed
and otherwise untrusted certificates, closing the MITM hole while keeping every
Bosch cloud call working.

Local camera endpoints (LAN IPs) use per-device self-signed certificates and
intentionally keep ``verify=False`` — that is a documented local-network
exception and is out of scope for this module.
"""

from __future__ import annotations

import ssl

# Bosch "Video CA 2A" intermediate CA, issued by the private "Bosch ST Root CA".
# Extracted from the live residential.cbs.boschsecurity.com certificate chain.
# Validity: 2021-03-18 .. 2057-03-20.
# SHA-256 fingerprint:
#   9F:6A:CB:6D:79:38:60:A3:B1:B4:37:EA:D3:A7:D5:A6:
#   28:D0:28:8E:24:41:52:A5:E9:C9:6B:36:51:D6:01:D1
BOSCH_CLOUD_CA_PEM = """\
-----BEGIN CERTIFICATE-----
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
nvQ8Em1LhUA=
-----END CERTIFICATE-----
"""

_SSL_CONTEXT: ssl.SSLContext | None = None


def bosch_cloud_ssl_context() -> ssl.SSLContext:
    """Return a cached SSL context trusting system roots plus the Bosch private CA.

    Builds an :class:`ssl.SSLContext` that:
    - loads the platform's default CA bundle (for public hosts such as the
      OAuth / Keycloak endpoint on Let's Encrypt),
    - additionally trusts the Bosch private intermediate CA (``Video CA 2A``)
      so that ``residential.cbs.boschsecurity.com`` and
      ``proxy-*.live.cbs.boschsecurity.com`` are accepted without disabling
      verification,
    - sets ``VERIFY_X509_PARTIAL_CHAIN`` so OpenSSL can anchor the Bosch chain
      at the intermediate rather than requiring the self-signed root to be in
      the trust store.

    This replaces ``verify=False`` / ``ssl=False`` on all cloud calls
    (CWE-295 / GHSA-6qh5-x5m5-vj6v).  Local camera LAN endpoints (self-signed,
    TOFU-pinned) are explicitly out of scope and keep their own ``verify=False``.

    The context is built once and cached for the lifetime of the process.
    """
    # process-lifetime SSL-context cache, set once
    global _SSL_CONTEXT  # pylint: disable=global-statement
    if _SSL_CONTEXT is None:
        ctx = ssl.create_default_context()
        ctx.load_verify_locations(cadata=BOSCH_CLOUD_CA_PEM)
        # The pinned Bosch CA is an intermediate (not a self-signed root), so
        # allow OpenSSL to anchor the chain at it.  This does not weaken
        # validation of public hosts: their chains still terminate at a trusted
        # system root.
        # present at runtime (Python 3.10+), pylint's ssl stub lags
        # pylint: disable-next=no-member
        ctx.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
        _SSL_CONTEXT = ctx
    return _SSL_CONTEXT
