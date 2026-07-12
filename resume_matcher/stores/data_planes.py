"""Code-enforced separation of the two data planes.

In production these are two separate databases with separate access control. Here they are two
classes that enforce the same invariant in code:

  * ScoringStore  — candidates, jobs, results. REJECTS any feature dict containing a protected
                    attribute or a known proxy. This is the boundary that keeps the system lawful.
  * AuditStore    — voluntary self-ID protected attributes, keyed by candidate_id, exposed ONLY as
                    aligned label lists for aggregate analysis. There is deliberately no method that
                    returns a candidate's protected attributes joined to their scoring features.
"""
from __future__ import annotations

from ..inference.schema import CandidateProfile, JobSpec, ScoreResult

# Attributes that must never influence a score, plus common proxies for them.
PROTECTED_KEYS = {
    "race", "ethnicity", "ethnic_origin", "ancestry", "place_of_origin", "national_origin",
    "color", "colour", "citizenship", "creed", "religion", "sex", "gender", "sexual_orientation",
    "age", "disability", "marital_status", "family_status",
    # deliberate proxies the audit watches and scoring must not encode:
    "name", "postal_code", "zip", "neighbourhood", "neighborhood", "first_language", "mother_tongue",
    "community",
}

# The only attributes the audit plane is allowed to store (voluntary self-ID).
AUDITABLE_ATTRIBUTES = {
    "race_ethnicity", "gender", "disability_status", "first_generation", "international_status",
}

# Relationship-graph structural features (Phase 4). Graph degree/reachability correlate with
# first_generation / international_status, so they are PROXIES: they may drive the positive-action
# mitigation program (adding opportunity to the under-networked), but they must NEVER enter a
# score or any opportunity-allocation decision that RANKS candidates. This guard extends the
# no-proxy guarantee beyond match_results to every scoring feature dict (adversarial requirement).
NETWORK_FEATURE_KEYS = {
    "degree", "reachability", "intro_count", "connector_count", "components", "network_poverty",
}


class ProtectedDataError(ValueError):
    pass


class ScoringStore:
    """Holds everything used to produce a score. Refuses protected attributes / proxies."""

    def __init__(self) -> None:
        self._candidates: dict[str, CandidateProfile] = {}
        self._jobs: dict[str, JobSpec] = {}
        self._results: dict[tuple[str, str], ScoreResult] = {}

    @staticmethod
    def assert_no_protected(features: dict) -> None:
        keys = {k.lower() for k in features}
        bad = PROTECTED_KEYS & keys
        if bad:
            raise ProtectedDataError(
                f"Refusing to store protected attribute(s)/proxy(ies) in the scoring plane: "
                f"{sorted(bad)}. These belong only in the AuditStore (aggregate-only)."
            )
        net = NETWORK_FEATURE_KEYS & keys
        if net:
            raise ProtectedDataError(
                f"Refusing to put relationship-graph feature(s) {sorted(net)} into a scoring "
                f"feature dict. Graph degree is a network-privilege proxy — it may drive the "
                f"positive-action mitigation program, never a ranking/score (boundary #2)."
            )

    def add_candidate(self, c: CandidateProfile, extra_features: dict | None = None) -> None:
        if extra_features:
            self.assert_no_protected(extra_features)
        self._candidates[c.candidate_id] = c

    def add_job(self, j: JobSpec) -> None:
        self._jobs[j.job_id] = j

    def add_result(self, r: ScoreResult) -> None:
        self._results[(r.candidate_id, r.job_id)] = r

    def candidate(self, cid: str) -> CandidateProfile | None:
        return self._candidates.get(cid)

    def candidate_ids(self) -> list[str]:
        return list(self._candidates)


class AuditStore:
    """Voluntary self-ID protected attributes. Aggregate-only egress; no per-candidate join."""

    MIN_CELL = 5  # never report on a group smaller than this

    def __init__(self) -> None:
        self._self_id: dict[str, dict[str, str]] = {}

    def record_self_id(self, candidate_id: str, attributes: dict[str, str]) -> None:
        bad = {k for k in attributes if k not in AUDITABLE_ATTRIBUTES}
        if bad:
            raise ProtectedDataError(
                f"AuditStore only accepts auditable self-ID attributes {sorted(AUDITABLE_ATTRIBUTES)}; "
                f"got disallowed key(s): {sorted(bad)}."
            )
        self._self_id.setdefault(candidate_id, {}).update(attributes)

    def labels_for(self, candidate_ids: list[str], attribute: str) -> list[str | None]:
        """Aligned label list (None where a candidate did not self-identify). The ONLY egress —
        returns labels for aggregate metrics, never joined to scoring features."""
        if attribute not in AUDITABLE_ATTRIBUTES:
            raise ProtectedDataError(f"Unknown auditable attribute: {attribute!r}")
        return [self._self_id.get(cid, {}).get(attribute) for cid in candidate_ids]

    def has_data(self) -> bool:
        return bool(self._self_id)
