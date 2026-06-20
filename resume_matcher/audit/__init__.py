"""Bias audit (plan §D). Measures whether the system's selections disadvantage protected groups —
the lawful 'detect & flag' version of the 'people hire from their own community' hunch. Protected
attributes are read ONLY from the AuditStore, ONLY in aggregate, and NEVER feed scoring."""
