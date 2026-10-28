"""Offline candidate for naming clear anonymous intervals, never whole clusters.

This module is deliberately not wired into transcription or publication. Promotion
requires independent human-labelled recordings and the speaker acceptance gates.
"""
from __future__ import annotations

import math
import numpy as np


def _unit(value):
    vector = np.asarray(value, dtype=np.float64)
    if vector.ndim != 1 or vector.size == 0 or not np.isfinite(vector).all():
        raise ValueError("Invalid speaker embedding")
    norm = np.linalg.norm(vector)
    if norm < 1e-9:
        raise ValueError("Empty speaker embedding")
    return vector / norm


def interval_identity(embedding, profiles, *, model, duration, overlapping=False):
    """Fixed candidate gates: >=4 seconds, cosine >=0.50, runner-up margin >=0.15.

    A declined interval stays anonymous; a matching interval provides no evidence
    for naming its neighbouring fragments or their shared diarization cluster.
    Invalid model/vector configuration raises rather than producing a known name.
    """
    if not isinstance(model, str) or not model or not math.isfinite(duration) or duration <= 0:
        raise ValueError("Invalid interval metadata")
    vector = _unit(embedding)
    scores = {}
    for profile in profiles:
        if profile.get("model") != model:
            raise ValueError("Speaker embedding model mismatch")
        name = profile.get("name")
        if not isinstance(name, str) or not name.strip() or name in scores:
            raise ValueError("Invalid or duplicate speaker profile")
        reference = _unit(profile["embedding"])
        if reference.shape != vector.shape:
            raise ValueError("Speaker embedding dimension mismatch")
        scores[name] = float(vector @ reference)
    ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    result = {"name": None, "reason": "no_profiles", "scores": dict(ranked)}
    if overlapping:
        return {**result, "reason": "overlap"}
    if duration < 4:
        return {**result, "reason": "short_interval"}
    if not ranked:
        return result
    if ranked[0][1] < .50:
        return {**result, "reason": "low_similarity"}
    if len(ranked) > 1 and ranked[0][1] - ranked[1][1] < .15:
        return {**result, "reason": "ambiguous_identity"}
    return {**result, "name": ranked[0][0], "reason": "clear_interval"}
