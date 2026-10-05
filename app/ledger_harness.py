"""Deterministischer Ledger-Harness für Modellvergleiche.

0.4.56 trennt bewusst Produktionslogik und Qualitätsmessung: Ein selektives Gold beschreibt atomare
Fakten und ausdrücklich verbotene Claims. Der Scorer arbeitet ausschließlich auf strukturierten Ledger-Feldern;
Stichwörter aus getrennten Events werden niemals zu einem Treffer zusammengesetzt.
"""
from __future__ import annotations

import json
import re
from typing import Any

import jsonschema


class LedgerGoldFehler(ValueError):
    pass


_ALIAS = {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}}
_GROUPS = {"type": "array", "items": {"anyOf": [_ALIAS, {"type": "string", "minLength": 1}]}}
_ASSERTION = {
    "type": "object",
    "properties": {
        "subject": _ALIAS,
        "property": {"anyOf": [{"type": "string", "minLength": 1}, _ALIAS]},
        "value": _ALIAS,
        "epistemic": _ALIAS,
    },
    "required": ["subject", "property", "value"],
    "additionalProperties": False,
}
_MATCH = {
    "type": "object",
    "properties": {
        "actors": _GROUPS,
        "targets": _GROUPS,
        "objects": _GROUPS,
        "locations": _GROUPS,
        "factions": _GROUPS,
        "kindsAny": {"type": "array", "items": {"type": "string"}},
        "tagsAll": {"type": "array", "items": {"type": "string"}},
        "assertions": {"type": "array", "items": _ASSERTION},
        "timeRange": {
            "type": "array", "minItems": 2, "maxItems": 2,
            "prefixItems": [{"type": "number", "minimum": 0}, {"type": "number", "minimum": 0}],
            "items": False,
        },
    },
    "minProperties": 1,
    "additionalProperties": False,
}
_EXPECTED = {
    "type": "object",
    "properties": {
        "epistemic": _ALIAS,
        "importance": _ALIAS,
        "relevance": {
            "type": "object",
            "properties": {"recap": {"type": "boolean"}, "openThread": {"type": "boolean"}, "bible": {"type": "boolean"}},
            "additionalProperties": False,
        },
        "tagsAll": {"type": "array", "items": {"type": "string"}},
    },
    "additionalProperties": False,
}
_ITEM = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "minLength": 1},
        "description": {"type": "string"},
        "importance": {"type": "string", "enum": ["critical", "important", "minor"]},
        "match": _MATCH,
        "expected": _EXPECTED,
    },
    "required": ["id", "match"],
    "additionalProperties": False,
}
GOLD_SCHEMA = {
    "type": "object",
    "properties": {
        "version": {"const": 1},
        "name": {"type": "string"},
        "scope": {"type": "string", "enum": ["selective", "exhaustive"]},
        "facts": {"type": "array", "items": _ITEM},
        "expectedNonClaims": {"type": "array", "items": _ITEM},
    },
    "required": ["version", "facts", "expectedNonClaims"],
    "additionalProperties": False,
}


def ledger_gold_lesen(text: str) -> dict:
    try:
        d = json.loads(text)
    except json.JSONDecodeError as e:
        raise LedgerGoldFehler(f"Ledger-Gold ist kein gültiges JSON: {e}") from e
    try:
        jsonschema.validate(d, GOLD_SCHEMA)
    except jsonschema.ValidationError as e:
        pfad = ".".join(str(x) for x in e.absolute_path)
        raise LedgerGoldFehler(f"Ungültiges Ledger-Gold{f' bei {pfad}' if pfad else ''}: {e.message}") from e
    ids = [x["id"] for x in (d.get("facts") or []) + (d.get("expectedNonClaims") or [])]
    if len(ids) != len(set(ids)):
        raise LedgerGoldFehler("IDs in facts/expectedNonClaims müssen eindeutig sein.")
    return d


def _norm(x: Any) -> str:
    return " ".join(re.findall(r"\w+", str(x or "").casefold(), flags=re.UNICODE))


def _alias_passt(actual: Any, aliases: list[str]) -> bool:
    a = _norm(actual)
    if not a:
        return False
    for alias in aliases:
        n = _norm(alias)
        if n and (a == n or n in a or a in n):
            return True
    return False


def _gruppen(v: Any) -> list[list[str]]:
    if not isinstance(v, list):
        return []
    aus = []
    for x in v:
        if isinstance(x, str):
            aus.append([x])
        elif isinstance(x, list):
            aus.append([str(y) for y in x if str(y).strip()])
    return [x for x in aus if x]


def _assertion_passt(a: dict, spec: dict) -> bool:
    if not isinstance(a, dict):
        return False
    if not _alias_passt(a.get("subject"), spec.get("subject") or []):
        return False
    props = spec.get("property") if isinstance(spec.get("property"), list) else [spec.get("property")]
    if str(a.get("property") or "") not in {str(x) for x in props if x}:
        return False
    if not _alias_passt(a.get("value"), spec.get("value") or []):
        return False
    epi = spec.get("epistemic") or []
    return not epi or str(a.get("epistemic") or "") in epi


def _event_pruefen(event: dict, spec: dict) -> tuple[bool, int, int, dict]:
    """Alle Bedingungen müssen im SELBEN Event liegen. score dient nur dazu, den besten Fehlkandidaten zu erklären."""
    score = total = 0
    details: dict[str, Any] = {}
    for feld in ("actors", "targets", "objects", "locations", "factions"):
        checks = []
        actual = event.get(feld) or []
        for aliases in _gruppen(spec.get(feld)):
            ok = any(_alias_passt(x, aliases) for x in actual)
            checks.append(ok)
            score += int(ok)
            total += 1
        if checks:
            details[feld] = checks

    if spec.get("timeRange"):
        start, end = spec["timeRange"]
        t = event.get("time")
        ok = isinstance(t, (int, float)) and start <= float(t) <= end
        details["timeRange"] = ok
        score += int(ok)
        total += 1

    if spec.get("kindsAny"):
        ok = any(str(x) in set(spec["kindsAny"]) for x in event.get("kinds") or [])
        details["kindsAny"] = ok
        score += int(ok)
        total += 1

    if spec.get("tagsAll"):
        tags = set(str(x) for x in event.get("tags") or [])
        for tag in spec["tagsAll"]:
            ok = tag in tags
            details.setdefault("tagsAll", []).append(ok)
            score += int(ok)
            total += 1

    for a_spec in spec.get("assertions") or []:
        ok = any(_assertion_passt(a, a_spec) for a in event.get("assertions") or [])
        details.setdefault("assertions", []).append(ok)
        score += int(ok)
        total += 1

    return bool(total and score == total), score, total, details


def _bestes_event(events: list[dict], spec: dict) -> tuple[dict | None, dict]:
    best = None
    best_detail = {"score": 0, "total": 0, "matched": False, "details": {}}
    for e in events:
        ok, score, total, details = _event_pruefen(e, spec)
        key = (int(ok), score, -total)
        best_key = (int(best_detail["matched"]), best_detail["score"], -best_detail["total"])
        if best is None or key > best_key:
            best = e
            best_detail = {"score": score, "total": total, "matched": ok, "details": details}
    return best, best_detail


def _ratio(ok: int, total: int) -> float | None:
    return round(ok / total, 4) if total else None


def _meta_entitaet(x: Any) -> bool:
    return bool(re.search(r"\b(?:spielleitung|spielleiter\w*|game\s*master|gamemaster|dungeon\s*master|\bgm\b)\b",
                          str(x or ""), re.IGNORECASE))


def _provenance_ok(event: dict, valid_source_ids: set[str] | None, source_revision: str | None) -> bool:
    ids = [str(x) for x in event.get("sourceIds") or []]
    if not ids or (valid_source_ids is not None and any(x not in valid_source_ids for x in ids)):
        return False
    ev = [x for x in event.get("evidence") or [] if isinstance(x, dict)]
    by_id = {str(x.get("sourceId") or ""): x for x in ev}
    for lid in ids:
        x = by_id.get(lid)
        if not x:
            return False
        if x.get("sourceType") != "TRANSCRIPT" or x.get("sourceLocation") != lid:
            return False
        if source_revision and x.get("sourceRevision") != source_revision:
            return False
    return True


def ledger_bewerten(ledger: dict, gold: dict, valid_source_ids: set[str] | None = None) -> dict:
    events = [e for e in ledger.get("events") or [] if isinstance(e, dict)]
    fact_results = []
    attr_ok = attr_total = rel_ok = rel_total = epi_ok = epi_total = relevance_ok = relevance_total = 0
    critical_ok = critical_total = important_ok = important_total = 0

    for fact in gold.get("facts") or []:
        event, detail = _bestes_event(events, fact["match"])
        matched = bool(event is not None and detail["matched"])
        importance = fact.get("importance") or "important"
        if importance == "critical":
            critical_total += 1
            critical_ok += int(matched)
        elif importance == "important":
            important_total += 1
            important_ok += int(matched)

        for feld in ("actors", "targets", "objects"):
            vals = detail.get("details", {}).get(feld) or []
            attr_total += len(vals)
            attr_ok += sum(bool(x) for x in vals)
        vals = detail.get("details", {}).get("assertions") or []
        rel_total += len(vals)
        rel_ok += sum(bool(x) for x in vals)

        checks = {}
        expected = fact.get("expected") or {}
        if "epistemic" in expected:
            epi_total += 1
            ok = bool(matched and str(event.get("epistemic") or "") in expected["epistemic"])
            epi_ok += int(ok)
            checks["epistemic"] = ok
        for k, soll in (expected.get("relevance") or {}).items():
            relevance_total += 1
            ok = bool(matched and (event.get("relevance") or {}).get(k) is soll)
            relevance_ok += int(ok)
            checks[f"relevance.{k}"] = ok
        if expected.get("importance"):
            checks["importance"] = bool(matched and event.get("importance") in expected["importance"])
        if expected.get("tagsAll"):
            tags = set(event.get("tags") or []) if event else set()
            checks["tagsAll"] = bool(matched and all(x in tags for x in expected["tagsAll"]))

        fact_results.append({
            "id": fact["id"], "description": fact.get("description") or "", "importance": importance,
            "matched": matched, "eventId": event.get("eventId") if event else None,
            "score": detail["score"], "checks": detail["total"], "dimensions": detail["details"],
            "expectedChecks": checks,
        })

    non_results = []
    for item in gold.get("expectedNonClaims") or []:
        treffer = []
        for e in events:
            ok, _score, _total, _details = _event_pruefen(e, item["match"])
            if ok:
                treffer.append(e.get("eventId"))
        non_results.append({
            "id": item["id"], "description": item.get("description") or "",
            "violated": bool(treffer), "eventIds": [x for x in treffer if x],
        })

    source_revision = str(ledger.get("sourceFingerprint") or "") or None
    prov_ok = sum(_provenance_ok(e, valid_source_ids, source_revision) for e in events)
    table_roles = 0
    for e in events:
        vals = [x for f in ("actors", "targets", "objects", "locations", "factions") for x in e.get(f) or []]
        vals += [a.get("subject") for a in e.get("assertions") or [] if isinstance(a, dict)]
        vals += [a.get("value") for a in e.get("assertions") or [] if isinstance(a, dict)]
        if any(_meta_entitaet(x) for x in vals):
            table_roles += 1

    fps = [str(e.get("eventFingerprint") or "") for e in events if e.get("eventFingerprint")]
    duplicate_fps = len(fps) - len(set(fps))
    matched = sum(1 for x in fact_results if x["matched"])
    violations = sum(1 for x in non_results if x["violated"])
    metrics = {
        "scope": gold.get("scope") or "selective",
        "factsMatched": matched,
        "factsTotal": len(fact_results),
        "factRecall": _ratio(matched, len(fact_results)),
        "criticalRecall": _ratio(critical_ok, critical_total),
        "importantRecall": _ratio(important_ok, important_total),
        "attributionAccuracy": _ratio(attr_ok, attr_total),
        "relationAccuracy": _ratio(rel_ok, rel_total),
        "epistemicAccuracy": _ratio(epi_ok, epi_total),
        "relevanceAccuracy": _ratio(relevance_ok, relevance_total),
        "expectedNonClaimViolations": violations,
        "expectedNonClaimsTotal": len(non_results),
        "provenanceCompleteness": _ratio(prov_ok, len(events)),
        "tableRoleWorldEntityCount": table_roles,
        "duplicateEventFingerprints": duplicate_fps,
    }
    gates = {
        "provenance100": (metrics["provenanceCompleteness"] == 1.0 if events else True),
        "noExpectedNonClaims": violations == 0,
        "noTableRolesAsWorldEntities": table_roles == 0,
        "noExactEventDuplicates": duplicate_fps == 0,
    }
    return {
        "version": 1,
        "goldName": gold.get("name") or "",
        "metrics": metrics,
        "gates": gates,
        "facts": fact_results,
        "expectedNonClaims": non_results,
    }


def harness_md(result: dict) -> str:
    m = result.get("metrics") or {}
    g = result.get("gates") or {}

    def pct(v):
        return "–" if v is None else f"{100 * float(v):.1f} %"

    lines = [
        f"# Ledger Harness – {result.get('goldName') or 'Gold v1'}", "",
        f"- Atomic facts: **{m.get('factsMatched', 0)}/{m.get('factsTotal', 0)}** ({pct(m.get('factRecall'))})",
        f"- Critical recall: **{pct(m.get('criticalRecall'))}**",
        f"- Important recall: **{pct(m.get('importantRecall'))}**",
        f"- Attribution: **{pct(m.get('attributionAccuracy'))}**",
        f"- Relation: **{pct(m.get('relationAccuracy'))}**",
        f"- Epistemik: **{pct(m.get('epistemicAccuracy'))}**",
        f"- Relevanz: **{pct(m.get('relevanceAccuracy'))}**",
        f"- Expected-non-claim violations: **{m.get('expectedNonClaimViolations', 0)}**",
        f"- Provenance completeness: **{pct(m.get('provenanceCompleteness'))}**",
        f"- Tischrollen als Weltentität: **{m.get('tableRoleWorldEntityCount', 0)}**",
        f"- Doppelte Event-Fingerprints: **{m.get('duplicateEventFingerprints', 0)}**",
        "", "## Gates",
    ]
    lines += [f"- {'PASS' if ok else 'FAIL'} – {name}" for name, ok in g.items()]
    lines += ["", "## Fakten"]
    for x in result.get("facts") or []:
        lines.append(f"- {'PASS' if x.get('matched') else 'MISS'} `{x.get('id')}`"
                     + (f" → {x.get('eventId')}" if x.get("eventId") else ""))
    if result.get("expectedNonClaims"):
        lines += ["", "## Expected non-claims"]
        for x in result["expectedNonClaims"]:
            lines.append(f"- {'FAIL' if x.get('violated') else 'PASS'} `{x.get('id')}`"
                         + (f" → {', '.join(x.get('eventIds') or [])}" if x.get("eventIds") else ""))
    return "\n".join(lines) + "\n"
