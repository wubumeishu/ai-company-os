"""Artifact / Evidence ledger reference reader — ``artifact://`` / ``evidence://``.

Implements the ref-scheme extension of the parent Artifact / Evidence design
(docs/architecture/PHASE_4_ARTIFACT_EVIDENCE_V1_DESIGN.md §4.1 / §11): the two
stable ledger refs ``artifact://{id}`` and ``evidence://{id}`` let a reviewer
or completion gate cite "the artifact" and "the proof" by stable id instead of
a blob key.

The frozen deterministic verifier
(``app/services/agent_runtime/verification.py`` — Phase 2C-3, card
``t_f19aae89``/``t_dbb0c0dd`` frozen list) accepts a *pluggable*
``ReferenceExists`` callable (its ``reference_exists`` / ``_reference_exists``
injection point).  This module is that extension **as a separate additive
reader** — it does NOT modify the frozen verifier, and it REUSES the frozen
reference-exists reader for every non-ledger scheme (``workspace://``,
``published-page://``, ``imagekit://``, ``http(s)://``, ``tool-result://``),
so a non-ledger ref falls through to the existing trusted reader.

A ledger ref resolves to a row and is *readable* when:

- the row exists (by ``id``) in the caller's tenant scope, and
- for ``artifact://`` the row is *current* (not superseded): a superseded row
  is a historical provenance link, not a currently-citable artifact — the
  re-verify check that the "current valid review" query gives (design §3.4),
- for ``evidence://`` the row is a verdict / evidence row (any ``kind``; the
  re-verify semantics live in the completion/review lane, not here).

The reader is deliberately the *deterministic* resolver extended with two new
refs (design §5.2): no LLM step, no second mechanism — it reads the two ledger
tables through the owning DAOs and delegates everything else.

Tenant isolation: the verifier's reader protocol passes an explicit
``tenant_id``, but the DAO's scoped reads resolve the tenant from the
``_tenant_ctx`` ContextVar (which the Runtime may not have bound).  Every
ledger read is therefore wrapped in ``tenant_context(tenant_id)`` so the
caller's tenant is the authority — a cross-tenant ledger ref is never
resolvable (design D6 / I-7).
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

from app.dao.artifact_evidence_dao import artifact_record_dao, evidence_record_dao
from app.dao.base import tenant_context

#: The frozen verifier's pluggable reader protocol:
#: ``reference_exists(reference, tenant_id, run_id) -> bool``.
ReferenceExists = Callable[[str, uuid.UUID, uuid.UUID], Awaitable[bool]]

# The two new ledger ref schemes (design §4.1).
ARTIFACT_REF_SCHEME = "artifact"
EVIDENCE_REF_SCHEME = "evidence"


def _ledger_ref_id(reference: str, scheme: str) -> uuid.UUID | None:
    """Parse ``scheme://{uuid}`` into its id, or ``None`` when malformed.

    Mirrors the frozen verifier's ``_stable_reference_id`` discipline: the id
    sits in the URL *netloc* (``artifact://<uuid>`` — exactly how the frozen
    reader reads ``published-page://{short_id}``), with no path/query/fragment,
    so a tampered ref cannot smuggle a second row id.  A malformed ref is not
    a ledger ref and falls through to the wrapped reader (which fails closed on
    an unknown scheme).
    """
    try:
        parsed = urlsplit(reference)
    except ValueError:
        return None
    if parsed.scheme != scheme or parsed.path or parsed.query or parsed.fragment:
        return None
    try:
        return uuid.UUID(parsed.netloc)
    except (ValueError, AttributeError):
        return None


class ArtifactLedgerReferenceReader:
    """Read back ``artifact://`` / ``evidence://`` ledger refs (deterministic).

    Conforms to the frozen verifier's ``ReferenceExists`` protocol so it can
    be passed as ``reference_exists`` to
    ``ToolLedgerRuntimeVerifier`` / ``RuntimeToolReferenceReader``.  Every
    non-ledger scheme is delegated to the optional ``wrapped`` reader (the
    frozen verifier's existing trusted reader), so nothing is re-implemented.
    """

    def __init__(self, *, wrapped: ReferenceExists | None = None) -> None:
        self._wrapped = wrapped

    async def reference_exists(
        self,
        reference: str,
        tenant_id: uuid.UUID,
        run_id: uuid.UUID,
    ) -> bool:
        """Return true for a resolvable ledger ref or the wrapped reader's answer.

        A ledger ref is readable iff the row exists in the caller's tenant
        scope.  For ``artifact://`` the row must also be *current* (not
        superseded) — a historical row is a provenance link, not a citable
        artifact.  A non-ledger ref delegates to the wrapped reader (fail-closed
        when no reader is wired).
        """
        if not isinstance(reference, str) or not reference.strip():
            return False
        ref = reference.strip()

        artifact_id = _ledger_ref_id(ref, ARTIFACT_REF_SCHEME)
        if artifact_id is not None:
            with tenant_context(tenant_id):
                row = await artifact_record_dao.get_scoped(artifact_id)
            if row is None:
                return False
            # A superseded artifact is historical (its ``superseded_by`` link
            # is the rework provenance edge), not a currently-citable record —
            # the same "current valid set" rule the current-valid-review query
            # uses (design §3.4, G2).  Re-verify reads the NEW row.
            return row.superseded_by is None

        evidence_id = _ledger_ref_id(ref, EVIDENCE_REF_SCHEME)
        if evidence_id is not None:
            with tenant_context(tenant_id):
                row = await evidence_record_dao.get_scoped(evidence_id)
            return row is not None

        # Not a ledger ref: delegate to the wrapped (frozen) reader when
        # present; otherwise the ref is untrusted on this lane (fail-closed).
        if self._wrapped is None:
            return False
        return await self._wrapped(ref, tenant_id, run_id)


__all__ = [
    "ARTIFACT_REF_SCHEME",
    "EVIDENCE_REF_SCHEME",
    "ArtifactLedgerReferenceReader",
    "ReferenceExists",
]
