"""Struktur-/Regressionsprüfungen mit expliziten Testantworten, kein E4B-Qualitätsnachweis."""
import copy
import json
from dataclasses import asdict
from pathlib import Path

import httpx
import pytest

from app.ledger_v2 import (Config, LedgerV2, SourceLine, discourse_windows,
                           fingerprint, transcript_lines, validate_event)
from app.ledger_v2_benchmark import (BenchmarkOllama, FIXTURES, _match, corpus_read,
                                    evaluate_case, evaluate_report, run_corpus)
from app.sprachmodell import Antwort


def source():
    return [SourceLine("L0001", 0, "SL", "Mara hält das Relikt."),
            SourceLine("L0002", 3, "SL", "Mara gibt Toren das Relikt."),
            SourceLine("L0003", 6, "Toren", "Ich nehme das Relikt.")]


def candidate():
    return {"candidateId": "test", "sourceIds": ["L0001", "L0002", "L0003"],
            "rawClaim": "Mara übergibt das Relikt an Toren.", "mentions": ["Mara", "Toren", "Relikt"],
            "typeHint": "TRANSFER"}


def event():
    return {"predicate": "TRANSFER", "participants": [
        {"entity": name, "role": role, "mention": name, "sourceId": "L0002", "resolution": "explicit"}
        for name, role in (("Mara", "source"), ("Toren", "recipient"), ("Relikt", "theme"))],
        "stateChanges": [{"subject": "Relikt", "property": "possession", "from": "Mara", "to": "Toren"}],
        "epistemic": "observed", "modality": "actual", "sourceIds": ["L0001", "L0002", "L0003"],
        "anchorId": "L0002", "eventTime": None}


class Fake:
    modell = "test-fixture-not-a-model"

    def __init__(self, change=None, verify=None, overflow=False):
        self.change, self.verify, self.overflow = change, verify, overflow
        self.requests = []

    def chat_strukturiert(self, system, user, schema):
        payload = json.loads(user)
        self.requests.append(payload)
        if "jobs" not in payload:
            c = candidate()
            c.pop("candidateId")
            return Antwort(json.dumps({"candidates": [c], "overflow": self.overflow}), 10, 10, "stop")
        decisions = []
        for job in payload["jobs"]:
            d = {"candidateId": job["candidate"]["candidateId"], "decision": "accept", "reason": "test",
                 "risks": [], "event": event()}
            change = self.verify if "previous" in job and self.verify else self.change
            if change:
                change(d)
            decisions.append(d)
        return Antwort(json.dumps({"decisions": decisions}), 10, 10, "stop")


def test_raw_segments_not_merged_or_sanitized():
    lines = transcript_lines("Header\n[0:01:02] SL: Danke, Mira.\n[0:01:03] SL: Ich bin Toren.\n")
    assert [l.time for l in lines] == [62, 63]
    assert [l.text for l in lines] == ["Danke, Mira.", "Ich bin Toren."]
    with pytest.raises(ValueError):
        transcript_lines("[abc] bad")


def test_windows_keep_question_answer_cover_every_source_and_progress():
    lines = [SourceLine(f"L{i}", i, "Mira" if i % 2 == 0 else "SL",
                        "Was macht die Wache?" if i % 2 == 0 else "Sie öffnet die Tür.") for i in range(30)]
    windows = discourse_windows(lines, 100, overlap=3)
    assert {l.sourceId for w in windows for l in w} == {l.sourceId for l in lines}
    for q, a in zip(lines[::2], lines[1::2]):
        assert any(q in w and a in w for w in windows)
    assert len(windows) <= len(lines)
    assert discourse_windows([SourceLine("long", 0, "SL", "x" * 500)], 100)[0][0].text == "x" * 500


def test_transfer_requires_complete_roles_and_consistent_possession():
    assert validate_event(event(), candidate(), source()) == []
    e = event()
    e["stateChanges"][0]["to"] = "Mara"
    assert "transfer_state_conflict" in validate_event(e, candidate(), source())
    e = event()
    e["participants"].pop(0)
    assert "required_role:source" in validate_event(e, candidate(), source())


@pytest.mark.parametrize("mutation,issue", [
    (lambda e: e["sourceIds"].append("L9999"), "unknown_source"),
    (lambda e: e.update(anchorId="L9999"), "anchor_outside_candidate"),
    (lambda e: e["participants"][0].update(mention="erfunden"), "unbacked_mention"),
    (lambda e: e["participants"][0].update(entity="Spielleitung"), "table_role_entity"),
    (lambda e: e["stateChanges"][0].update(to="SL"), "table_role_state"),
    (lambda e: e["participants"][0].update(resolution="unresolved"), "unresolved_entity"),
    (lambda e: e["participants"][0].update(entity="Erfundene Person"), "entity_not_locally_grounded"),
])
def test_evidence_and_entity_validation(mutation, issue):
    e = event()
    mutation(e)
    assert issue in validate_event(e, candidate(), source())


def test_internally_consistent_reverse_is_not_semantically_proven():
    e = event()
    e["participants"][0].update(role="recipient")
    e["participants"][1].update(role="source")
    e["stateChanges"][0].update(**{"from": "Toren", "to": "Mara"})
    # Wichtige Grenze: Strukturvalidator kann dies NICHT aus der Sprache beweisen.
    assert validate_event(e, candidate(), source()) == []
    assert not _match(e, {"predicate": ["TRANSFER"], "participants": {"source": ["Mara"], "recipient": ["Toren"]}})


def test_contradictory_states_cannot_enter_one_atomic_event():
    e = event()
    e["stateChanges"].append({"subject": "Relikt", "property": "possession", "from": "Mara", "to": "Mara"})
    assert "contradictory_states_in_event" in validate_event(e, candidate(), source())


def test_no_false_positive_from_wrong_action_partial_name_or_negation():
    e = event()
    assert not _match(e, {"predicate": ["RESCUE"], "participants": {"rescuer": ["Mara"]}})
    assert not _match(e, {"participants": {"source": ["Mar"]}})
    e["stateChanges"] = [{"subject": "Mara", "property": "life_status", "from": None, "to": "not dead"}]
    assert not _match(e, {"stateChanges": [{"subject": ["Mara"], "property": ["life_status"], "to": ["dead"]}]})


def test_pipeline_provenance_mentions_and_no_gold_in_prompt():
    fake = Fake()
    result = LedgerV2(fake).run(source())
    assert result["state"] == "ok"
    assert len(result["events"]) == 1 and len(result["calls"]) == 2
    e = result["events"][0]
    assert e["time"] == 3
    assert all(p["mentionId"].startswith("M") for p in e["participants"])
    assert e["evidence"][1]["text"] == source()[1].text
    assert all(p["sourceRevision"] == result["sourceFingerprint"] for p in e["evidence"])
    assert "facts" not in json.dumps(fake.requests)
    assert "expectedNonClaims" not in json.dumps(fake.requests)


def test_risk_is_held_and_one_successful_local_verify_can_release_it():
    def broken(d):
        d["event"]["stateChanges"][0]["to"] = "Mara"
    fake = Fake(change=broken, verify=lambda d: None)
    result = LedgerV2(fake).run(source())
    assert result["state"] == "ok"
    assert [c["phase"] for c in result["calls"]] == ["evidence", "compile", "verify"]
    assert len(result["events"]) == 1
    assert result["audit"][0]["outcome"] == "held"


def test_unresolved_risk_never_enters_ledger_and_never_retries_forever():
    def risky(d):
        d["risks"] = ["coreference"]
    result = LedgerV2(Fake(change=risky)).run(source())
    assert result["events"] == [] and result["state"] == "uncertain"
    assert len(result["calls"]) == 3
    assert result["unresolved"]


def test_budget_failure_and_overflow_cannot_look_complete():
    result = LedgerV2(Fake(), Config(max_calls=1)).run(source())
    assert result["state"] == "incomplete" and not result["events"]
    assert len(result["calls"]) == 1
    result = LedgerV2(Fake(overflow=True)).run(source())
    assert result["state"] == "incomplete"
    assert result["failures"][0]["error"] == "candidate_overflow"


def test_append_only_dedupe_source_revisions_and_conflict_history():
    first = LedgerV2(Fake()).run(source())
    old = copy.deepcopy(first)
    second = LedgerV2(Fake()).run(source(), previous=first)
    assert first == old and len(second["events"]) == 1
    assert second["audit"][-1]["outcome"] == "duplicate"
    later = source()
    later[1] = SourceLine("L0002", 30, "SL", "Toren gibt Mara das Relikt zurück.")
    def reverse(d):
        d["event"]["participants"][0]["role"] = "recipient"
        d["event"]["participants"][1]["role"] = "source"
        d["event"]["stateChanges"][0].update(**{"from": "Toren", "to": "Mara"})
    third = LedgerV2(Fake(change=reverse)).run(later, previous=second)
    assert len(third["events"]) == 2 and len(third["stateHistory"]) == 2
    assert third["conflicts"][0]["status"] == "unresolved"
    with pytest.raises(ValueError):
        LedgerV2(Fake()).run(later, source_revision=first["sourceFingerprint"], previous=first)


def test_omitted_compiler_decision_is_not_deliberate_abstention():
    class Missing(Fake):
        def chat_strukturiert(self, system, user, schema):
            if "jobs" in json.loads(user):
                return Antwort('{"decisions": []}')
            return super().chat_strukturiert(system, user, schema)
    result = LedgerV2(Missing()).run(source())
    assert result["state"] == "incomplete" and not result["events"]
    assert result["failures"][0]["error"] == "missing_or_duplicate_decision"


def test_original_corpus_integrity_and_gold_not_executed_as_core_rules():
    corpus = corpus_read(FIXTURES / "mini_v2.json")
    assert len(corpus["cases"]) == 20
    assert sum(len(c["facts"]) for c in corpus["cases"] if c["kind"] == "real") == 10
    source_ids = [line["sourceId"] for c in corpus["cases"] if c["kind"] == "real" for line in c["lines"]]
    assert len(source_ids) == len(set(source_ids))
    core = Path(__file__).parents[1] / "app" / "ledger_v2.py"
    for name in ("Kano", "Pipo", "Lostriana", "Satuna", "Altvater", "Nostria", "Grünkappen", "Arborn"):
        assert name not in core.read_text(encoding="utf-8")


def test_gate_never_passes_unmeasured_fixture_or_partial_corpus():
    corpus = corpus_read(FIXTURES / "mini_v2.json")
    # Vollständig ideale Score-Zeilen testen nur die Gate-Logik, nicht das Modell.
    results = [{"caseId": c["id"], "kind": c["kind"], "facts": [],
                "expectedNonClaims": [{"id": n["id"], "eventIds": []} for n in c["expectedNonClaims"]],
                "metrics": {"factsMatched": len(c["facts"]), "factsTotal": len(c["facts"]),
                            "evidenceLocated": len(c["facts"]), "nonClaimViolations": 0},
                "gates": {"complete": True}} for c in corpus["cases"]]
    assert not evaluate_report(results, corpus, live=False, metadata={"calls": 40})["fullSessionEligible"]
    assert not evaluate_report(results[:-1], corpus, live=True, metadata={"calls": 38})["fullSessionEligible"]
    assert evaluate_report(results, corpus, live=True, metadata={"calls": 40})["fullSessionEligible"]
    assert not evaluate_report(results, corpus, live=True, metadata={"calls": 51})["fullSessionEligible"]


def test_scorer_requires_gold_evidence_not_just_matching_names():
    ledger = LedgerV2(Fake()).run(source())
    c = {"id": "unit", "kind": "synthetic", "lines": [asdict(l) for l in source()],
         "sourceRevision": ledger["sourceFingerprint"], "expectedNonClaims": [],
         "facts": [{"id": "transfer", "evidenceGroups": [["L9999"]],
                    "match": {"predicate": ["TRANSFER"], "participants": {"source": ["Mara"]}}}]}
    assert evaluate_case(c, ledger)["metrics"]["factsMatched"] == 0
    c["facts"][0]["evidenceGroups"] = [["L0002"]]
    assert evaluate_case(c, ledger)["metrics"]["factsMatched"] == 1
    ledger["events"][0]["evidence"][1]["text"] = "Falscher Text"
    assert not evaluate_case(c, ledger)["gates"]["provenance100"]


def test_ollama_transport_has_one_generation_attempt_and_pins_options():
    requests = []
    def handler(request):
        body = json.loads(request.content)
        requests.append((request.url.path, body))
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"capabilities": ["thinking"]})
        return httpx.Response(500, json={"error": "repeat limit reached"})
    c = BenchmarkOllama("http://ollama", "unit", 24576, httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(Exception, match="repeat limit"):
        c.chat_strukturiert("system", "user", {"type": "object"})
    generations = [body for path, body in requests if path == "/api/chat"]
    assert len(generations) == 1
    assert generations[0]["options"]["temperature"] == 0
    assert generations[0]["options"]["seed"] == 42
    assert generations[0]["think"] is False


def test_runner_writes_auditable_results_and_cannot_call_fake_run_live(tmp_path):
    lines = [asdict(l) for l in source()]
    case = {"id": "unit", "kind": "synthetic", "lines": lines, "sourceRevision": fingerprint(lines),
            "windowFingerprint": fingerprint(lines), "facts": [], "expectedNonClaims": []}
    target = tmp_path / "out"
    report = run_corpus(Fake(), {"version": 2, "cases": [case]}, target, metadata={}, progress=lambda _: None)
    assert not report["live"] and not report["fullSessionEligible"]
    assert json.loads((target / "case-01.json").read_text())["ledger"]["calls"][0]["rawResponse"]
    assert (target / "bericht.md").exists()
