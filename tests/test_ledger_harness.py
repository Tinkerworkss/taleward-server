"""0.4.56 Ledger-Harness: atomare Fakten, expected non-claims, Provenance und Modellkonsistenz."""
import json

import pytest


def _event(event_id="E0001", *, actor="Pipo", target="Gruppe", obj="Lostriana",
           subject="Lostriana", prop="possession", value="bei der Gruppe"):
    return {
        "eventId": event_id,
        "sourceIds": ["L0001"],
        "summary": "Pipo übergibt Lostriana an die Gruppe.",
        "kinds": ["possession"],
        "actors": [actor] if actor else [],
        "targets": [target] if target else [],
        "objects": [obj] if obj else [],
        "locations": [],
        "factions": [],
        "assertions": [{
            "subject": subject, "property": prop, "value": value,
            "epistemic": "observed", "certainty": "high",
        }],
        "epistemic": "observed",
        "modality": "actual",
        "importance": "critical",
        "relevance": {"recap": True, "openThread": False, "bible": True},
        "tags": [],
        "semanticFingerprint": "sem-1",
        "eventFingerprint": "event-1",
        "evidence": [{
            "sourceType": "TRANSCRIPT", "sourceId": "L0001", "sourceLocation": "L0001",
            "sourceRevision": "src-1", "relation": "SUPPORTS", "timestampStart": 1.0,
            "speakerLabel": "Spielleitung", "excerptRef": "L0001", "text": "[0:01] Spielleitung: Nehmt es.",
        }],
    }


def _gold():
    return {
        "version": 1,
        "name": "Harness Unit",
        "scope": "selective",
        "facts": [{
            "id": "transfer",
            "importance": "critical",
            "match": {
                "actors": [["Pipo"]],
                "targets": [["Gruppe", "Helden"]],
                "objects": [["Lostriana", "Schwert"]],
                "assertions": [{
                    "subject": ["Lostriana", "Schwert"], "property": "possession",
                    "value": ["Gruppe", "bei der Gruppe"],
                }],
            },
            "expected": {
                "epistemic": ["observed"],
                "relevance": {"recap": True, "bible": True},
            },
        }],
        "expectedNonClaims": [{
            "id": "reverse",
            "match": {
                "actors": [["Gruppe", "Helden"]],
                "targets": [["Pipo"]],
                "objects": [["Lostriana", "Schwert"]],
            },
        }],
    }


def test_gold_schema_und_eindeutige_ids():
    from app.ledger_harness import LedgerGoldFehler, ledger_gold_lesen

    d = _gold()
    assert ledger_gold_lesen(json.dumps(d))["facts"][0]["id"] == "transfer"
    d["expectedNonClaims"][0]["id"] = "transfer"
    with pytest.raises(LedgerGoldFehler):
        ledger_gold_lesen(json.dumps(d))


def test_atomarer_treffer_muss_im_selben_event_liegen():
    from app.ledger_harness import ledger_bewerten

    a = _event("E0001", target=None, obj=None)
    a["eventFingerprint"] = "a"
    b = _event("E0002", actor=None)
    b["eventFingerprint"] = "b"
    ledger = {"sourceFingerprint": "src-1", "events": [a, b]}
    r = ledger_bewerten(ledger, _gold(), {"L0001"})
    assert r["metrics"]["factsMatched"] == 0
    assert r["facts"][0]["matched"] is False


def test_relation_attribution_relevance_und_provenance_werden_getrennt_gemessen():
    from app.ledger_harness import ledger_bewerten

    ledger = {"sourceFingerprint": "src-1", "events": [_event()]}
    r = ledger_bewerten(ledger, _gold(), {"L0001"})
    m = r["metrics"]
    assert m["factRecall"] == 1.0
    assert m["criticalRecall"] == 1.0
    assert m["attributionAccuracy"] == 1.0
    assert m["relationAccuracy"] == 1.0
    assert m["epistemicAccuracy"] == 1.0
    assert m["relevanceAccuracy"] == 1.0
    assert m["provenanceCompleteness"] == 1.0
    assert m["expectedNonClaimViolations"] == 0
    assert all(r["gates"].values())


def test_expected_non_claim_findet_invertierte_uebergabe():
    from app.ledger_harness import ledger_bewerten

    falsch = _event(actor="Gruppe", target="Pipo", obj="Lostriana")
    falsch["eventFingerprint"] = "reverse"
    ledger = {"sourceFingerprint": "src-1", "events": [falsch]}
    r = ledger_bewerten(ledger, _gold(), {"L0001"})
    assert r["metrics"]["expectedNonClaimViolations"] == 1
    assert r["gates"]["noExpectedNonClaims"] is False


def test_provenance_und_tischrollen_sind_harte_safety_gates():
    from app.ledger_harness import ledger_bewerten

    e = _event(actor="Spielleitung")
    e["evidence"][0].pop("sourceRevision")
    ledger = {"sourceFingerprint": "src-1", "events": [e]}
    r = ledger_bewerten(ledger, _gold(), {"L0001"})
    assert r["metrics"]["provenanceCompleteness"] == 0.0
    assert r["metrics"]["tableRoleWorldEntityCount"] == 1
    assert r["gates"]["provenance100"] is False
    assert r["gates"]["noTableRolesAsWorldEntities"] is False


def test_exakte_event_duplikate_werden_diagnostiziert_semantische_wiederholung_aber_nicht_automatisch_geloescht():
    from app.ledger_harness import ledger_bewerten

    a = _event("E0001")
    b = _event("E0002")
    b["semanticFingerprint"] = a["semanticFingerprint"]
    b["eventFingerprint"] = a["eventFingerprint"]
    ledger = {"sourceFingerprint": "src-1", "events": [a, b]}
    r = ledger_bewerten(ledger, _gold(), {"L0001"})
    assert r["metrics"]["duplicateEventFingerprints"] == 1
    assert r["gates"]["noExactEventDuplicates"] is False


def test_modellvergleich_paarvergleich_nutzt_gold_als_referenz_nicht_das_andere_modell():
    from app import modellvergleich as mv

    a = mv.Ergebnis(modell="gemma4:e4b", kontext=24576, ok=True)
    b = mv.Ergebnis(modell="gemma4:12b", kontext=24576, ok=True)
    a.ledger = {"sourceFingerprint": "src", "events": [
        {"semanticFingerprint": "1"}, {"semanticFingerprint": "2"}
    ]}
    b.ledger = {"sourceFingerprint": "src", "events": [
        {"semanticFingerprint": "1"}, {"semanticFingerprint": "3"}
    ]}
    a.ledger_harness = {"facts": [{"id": "x", "matched": True}, {"id": "y", "matched": False}]}
    b.ledger_harness = {"facts": [{"id": "x", "matched": True}, {"id": "y", "matched": True}]}
    r = mv.ledger_paarvergleich([a, b])
    assert len(r) == 1
    assert r[0]["sameSourceFingerprint"] is True
    assert r[0]["semanticEventJaccard"] == pytest.approx(1 / 3, abs=0.0001)
    assert r[0]["goldFactAgreement"] == 0.5
