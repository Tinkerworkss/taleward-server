"""Kleiner E4B-Benchmark, ohne Serverlogin, Cloud-Aufruf oder Vollsession-Modus."""
from __future__ import annotations

import argparse
import json
import platform
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import httpx

from app.ledger_harness import ledger_bewerten
from app.ledger_v2 import (Config, LedgerV2, PARSER_VERSION, SourceLine, fingerprint,
                           legacy_view, norm, table_role)
from app.sprachmodell import Antwort, OllamaKlient, SprachmodellFehler

FIXTURES = Path(__file__).parent / "ledger_fixtures"


class BenchmarkOllama(OllamaKlient):
    """Genau ein /api/chat-Versuch pro gezähltem Aufruf, keine Modellinstallation."""

    def chat_strukturiert(self, system: str, nutzer: str, schema: dict) -> Antwort:
        body = {"model": self.modell, "stream": True, "keep_alive": "5m", "format": schema,
                "options": {"num_ctx": self.kontext, "temperature": 0, "seed": 42,
                            "num_predict": 4096, "repeat_penalty": 1.05},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": nutzer}]}
        if self.denkmodus():
            body["think"] = False
        # Dieselbe konservative Schätzung wie im bestehenden deutschen Parser.
        if (len(system) + len(nutzer) + len(json.dumps(schema))) / 3.2 + 4096 > self.kontext:
            raise SprachmodellFehler("Auftrag überschreitet das Kontextbudget.", erneut=False)
        status, error, data = self._streamen(body)
        if status >= 400 or error:
            raise SprachmodellFehler(f"Ollama: {status} {error[:300]}", erneut=False)
        if not data.get("done"):
            raise SprachmodellFehler("Ollama-Stream endete ohne Abschluss.", erneut=False)
        return Antwort(data.get("inhalt", ""), int(data.get("prompt_eval_count") or 0),
                       int(data.get("eval_count") or 0), data.get("done_reason"))


def corpus_read(path: Path) -> dict:
    corpus = json.loads(path.read_text(encoding="utf-8"))
    if corpus.get("version") != 2 or not isinstance(corpus.get("cases"), list) or not corpus["cases"]:
        raise ValueError("Ungültiger Mini-Corpus")
    ids, facts = set(), set()
    for case in corpus["cases"]:
        if case["id"] in ids:
            raise ValueError("Doppelte Fall-ID")
        ids.add(case["id"])
        lines = [SourceLine(**line) for line in case["lines"]]
        sources = {line.sourceId for line in lines}
        if len(lines) != len(sources) or not lines:
            raise ValueError("Leerer Fall oder doppelte Source-ID")
        if fingerprint(case["lines"]) != case["windowFingerprint"]:
            raise ValueError("Fenster-Fingerprint stimmt nicht")
        for fact in case["facts"]:
            if fact["id"] in facts:
                raise ValueError("Doppelte Fakt-ID")
            facts.add(fact["id"])
            if not fact.get("evidenceGroups") or any(not group or not set(group) <= sources
                                                     for group in fact["evidenceGroups"]):
                raise ValueError("Goldbeleg liegt außerhalb des Originalfensters")
    return corpus


def _value(actual: str | None, aliases: list[str]) -> bool:
    return norm(actual or "") in {norm(alias) for alias in aliases}


def _match(event: dict, spec: dict) -> bool:
    """Alle Rollen/Zustände im selben Event. Namen und Statuswerte nur exakte Aliasse."""
    if "anyOf" in spec:
        return any(_match(event, alternative) for alternative in spec["anyOf"])
    for field in ("predicate", "epistemic", "modality"):
        if spec.get(field) and event.get(field) not in spec[field]:
            return False
    for role, aliases in spec.get("participants", {}).items():
        if not any(p["role"] == role and _value(p["entity"], aliases) for p in event.get("participants", [])):
            return False
    for expected in spec.get("stateChanges", []):
        def state_matches(state):
            for key in ("subject", "property", "from", "to"):
                if key in expected and not _value(state.get(key), expected[key]):
                    return False
            if "toTokensAny" in expected:
                words = set(norm(state.get("to", "")).split())
                if words & {"nicht", "kein", "keine", "keinen", "not", "never"}:
                    return False
                if not any(set(norm(phrase).split()) <= words for phrase in expected["toTokensAny"]):
                    return False
            return True
        if not any(state_matches(s) for s in event.get("stateChanges", [])):
            return False
    return True


def _evidence(ids, groups) -> bool:
    return bool(groups) and all(set(ids) & set(group) for group in groups)


def evaluate_case(case: dict, ledger: dict) -> dict:
    facts = []
    for fact in case["facts"]:
        candidate_ids = [c["candidateId"] for c in ledger["candidates"]
                         if _evidence(c["sourceIds"], fact["evidenceGroups"])]
        matches = [e["eventId"] for e in ledger["events"] if _match(e, fact["match"])
                   and _evidence(e["sourceIds"], fact["evidenceGroups"])]
        facts.append({"id": fact["id"], "matched": bool(matches), "eventIds": matches,
                      "evidenceLocated": bool(candidate_ids), "candidateIds": candidate_ids})
    nonclaims = [{"id": item["id"], "eventIds": [e["eventId"] for e in ledger["events"] if _match(e, item["match"])]}
                 for item in case["expectedNonClaims"]]
    source = {line["sourceId"]: line for line in case["lines"]}
    provenance = True
    for event in ledger["events"]:
        evidence = {e["sourceId"]: e for e in event["evidence"]}
        for sid in event["sourceIds"]:
            original, actual = source.get(sid), evidence.get(sid, {})
            if not original or actual.get("text") != original["text"] or actual.get("speakerLabel") != original["speaker"] \
                    or actual.get("timestampStart") != original["time"] or actual.get("sourceType") != "TRANSCRIPT" \
                    or actual.get("sourceRevision") != case["sourceRevision"]:
                provenance = False
    fps = [e["eventFingerprint"] for e in ledger["events"]]
    meta = sum(any(table_role(p["entity"]) for p in e["participants"]) or
               any(table_role(s["subject"]) or table_role(s["to"]) or table_role(s["from"] or "")
                   for s in e["stateChanges"]) for e in ledger["events"])
    return {"caseId": case["id"], "kind": case["kind"], "facts": facts, "expectedNonClaims": nonclaims,
            "metrics": {"factsMatched": sum(f["matched"] for f in facts), "factsTotal": len(facts),
                        "evidenceLocated": sum(f["evidenceLocated"] for f in facts),
                        "compiledGivenEvidence": sum(f["matched"] and f["evidenceLocated"] for f in facts),
                        "nonClaimViolations": sum(bool(n["eventIds"]) for n in nonclaims),
                        "nonClaimsTotal": len(nonclaims)},
            "gates": {"provenance100": provenance, "noTableRoles": meta == 0,
                      "noExactDuplicates": len(fps) == len(set(fps)), "complete": ledger["state"] != "incomplete"}}


def evaluate_report(results: list[dict], corpus: dict, *, live: bool, metadata: dict) -> dict:
    real = [r for r in results if r["kind"] == "real"]
    synth = [r for r in results if r["kind"] == "synthetic"]
    expected_cases = {c["id"] for c in corpus["cases"]}
    actual_cases = {r["caseId"] for r in results}
    matched = sum(r["metrics"]["factsMatched"] for r in real)
    total = sum(len(c["facts"]) for c in corpus["cases"] if c["kind"] == "real")
    all_non = {n["id"] for r in real for n in r["expectedNonClaims"]}
    violated = {n["id"] for r in real for n in r["expectedNonClaims"] if n["eventIds"]}
    expected_non = {n["id"] for c in corpus["cases"] if c["kind"] == "real" for n in c["expectedNonClaims"]}
    gates = {"liveModelRun": live, "allCasesMeasured": actual_cases == expected_cases and len(results) == len(corpus["cases"]),
             "realFacts8of10": total == 10 and matched >= 8,
             "realNonClaims0of5": len(expected_non) == 5 and all_non == expected_non and not violated,
             "integrity": bool(results) and all(all(r["gates"].values()) for r in results),
             "syntheticNonClaims": not any(r["metrics"]["nonClaimViolations"] for r in synth),
             "withinCallBudget": metadata.get("calls", 51) <= 50}
    return {"version": 2, "parserVersion": PARSER_VERSION, "live": live, "metadata": metadata,
            "corpusFingerprint": fingerprint(corpus), "results": results,
            "metrics": {"realFactsMatched": matched, "realFactsTotal": total,
                        "realNonClaimViolations": len(violated), "realNonClaimsTotal": len(expected_non),
                        "syntheticFactsMatched": sum(r["metrics"]["factsMatched"] for r in synth),
                        "syntheticFactsTotal": sum(len(c["facts"]) for c in corpus["cases"] if c["kind"] == "synthetic"),
                        "evidenceLocated": sum(r["metrics"]["evidenceLocated"] for r in results)},
            "gates": gates, "fullSessionEligible": all(gates.values())}


def run_corpus(client, corpus: dict, target: Path, *, metadata: dict, progress=print,
               live: bool = False) -> dict:
    target.mkdir(parents=True, exist_ok=False)
    results, calls, verifies = [], 0, 0
    report = None
    legacy_events, legacy_sources = [], set()
    for index, case in enumerate(corpus["cases"]):
        if calls >= 50:
            break
        progress(f"[{index + 1}/{len(corpus['cases'])}] {case['id']}")
        config = Config(max_calls=50 - calls, max_verify_calls=max(0, 2 - verifies))
        ledger = LedgerV2(client, config, progress).run([SourceLine(**line) for line in case["lines"]],
                                                       source_revision=case["sourceRevision"])
        calls += len(ledger["calls"])
        verifies += sum(c["phase"] == "verify" for c in ledger["calls"])
        result = evaluate_case(case, ledger)
        results.append(result)
        path = target / f"case-{index + 1:02d}.json"
        path.write_text(json.dumps({"caseId": case["id"], "ledger": ledger, "evaluation": result},
                                   ensure_ascii=False, indent=2), encoding="utf-8")
        progress(f"  Fakten {result['metrics']['factsMatched']}/{result['metrics']['factsTotal']}; "
                 f"Non-Claims {result['metrics']['nonClaimViolations']}; {ledger['state']}")
        if case["kind"] == "real":
            legacy_events += legacy_view(ledger)["events"]
            legacy_sources.update(l["sourceId"] for l in case["lines"])
        report = evaluate_report(results, corpus, live=live, metadata={**metadata, "calls": calls})
        # Nach jedem Fenster ein verwertbarer Zwischenstand, auch bei Abbruch.
        (target / "bericht.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        # Bei Transport-/Formatfehlern kein nutzloser 20-Fenster-Retry-Sturm.
        if any(not c["ok"] for c in ledger["calls"]):
            break
    if report is None:
        raise ValueError("Keine Fälle ausgeführt")
    if corpus.get("legacyGold"):
        legacy = ledger_bewerten({"events": legacy_events, "sourceFingerprint": corpus["sourceRevision"]},
                                 corpus["legacyGold"], legacy_sources)
        (target / "legacy-harness.json").write_text(json.dumps(legacy, ensure_ascii=False, indent=2), encoding="utf-8")
    m = report["metrics"]
    text = ["# Ledger v2 Mini-Benchmark", "", f"- Reale Fakten: {m['realFactsMatched']}/{m['realFactsTotal']}",
            f"- Non-Claim-Verstöße: {m['realNonClaimViolations']}/{m['realNonClaimsTotal']}",
            f"- Synthetische Fakten: {m['syntheticFactsMatched']}/{m['syntheticFactsTotal']}",
            f"- Modellaufrufe: {calls}/50", f"- Volltest freigegeben: {report['fullSessionEligible']}", "",
            "Die Auswahl misst bekannte Fehlerklassen, keine Gesamtpräzision oder Laufzeit einer Vollsession.",
            "Ein strukturell valider Quellenbeleg beweist keine semantisch richtige Interpretation.", "", "## Gates", ""]
    text += [f"- {'PASS' if ok else 'FAIL'}: {gate}" for gate, ok in report["gates"].items()]
    text += ["", "## Einzelfakten", ""]
    for result in results:
        text += [f"- {'PASS' if f['matched'] else 'MISS'} {f['id']} "
                 f"(Evidenz lokalisiert: {f['evidenceLocated']})" for f in result["facts"]]
    (target / "bericht.md").write_text("\n".join(text) + "\n", encoding="utf-8")
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Ledger v2: nur Mini-Corpus, keine Vollsession und keine Serveränderung.")
    parser.add_argument("--modell", default="gemma4:e4b")
    parser.add_argument("--kontext", type=int, default=24576)
    parser.add_argument("--ollama", help="Ollama-Adresse; sonst localhost:11434/11435")
    parser.add_argument("--hardware", default="unknown", help="z.B. RTX 3060 Ti 8GB / CUDA")
    parser.add_argument("--ziel", type=Path, default=Path("ledger-v2-ergebnisse"))
    parser.add_argument("--corpus", type=Path, default=FIXTURES / "mini_v2.json")
    parser.add_argument("--inspect", action="store_true", help="Corpus prüfen, keine Modellaufrufe")
    args = parser.parse_args(argv)
    try:
        corpus = corpus_read(args.corpus)
        if args.inspect:
            print(json.dumps({"cases": len(corpus["cases"]),
                              "realFacts": sum(len(c["facts"]) for c in corpus["cases"] if c["kind"] == "real"),
                              "corpusFingerprint": fingerprint(corpus), "liveModelRun": False,
                              "fullSessionEligible": False}, ensure_ascii=False, indent=2))
            return 0
        from app.modellvergleich import ollama_finden
        # Dedizierter lokaler/LAN-Dienst: Betriebssystem-/Cloud-Proxys dürfen die
        # Loopback-Anfrage nicht umleiten oder zusätzliche SOCKS-Pakete voraussetzen.
        transport = httpx.Client(timeout=httpx.Timeout(180.0, connect=3.0), trust_env=False)
        try:
            url = ollama_finden(args.ollama, client=transport)
            client = BenchmarkOllama(url, args.modell, args.kontext, client=transport)
            response = client.client.get(f"{url}/api/tags")
            response.raise_for_status()
            model = next((m for m in response.json().get("models", [])
                          if args.modell in (m.get("name"), m.get("model"))), None)
            if not model or not model.get("digest"):
                raise ValueError(f"Modell {args.modell} ist nicht installiert oder hat keinen Digest.")
            target = args.ziel / ("mini-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f"))
            metadata = {"model": args.modell, "modelDigest": model["digest"], "context": args.kontext,
                        "hardware": args.hardware, "platform": platform.platform(), "ollama": client.version(),
                        "temperature": 0, "seed": 42, "timestamp": datetime.now(timezone.utc).isoformat(),
                        "parserFingerprint": fingerprint([Path(__file__).with_name("ledger_v2.py").read_text(encoding="utf-8"),
                                                          Path(__file__).read_text(encoding="utf-8")])}
            report = run_corpus(client, corpus, target, metadata=metadata, live=True)
            print(f"Ergebnis: {target.resolve()}")
            print("Mini-Gate bestanden." if report["fullSessionEligible"] else "Mini-Gate NICHT bestanden; kein Volltest.")
            return 0 if report["fullSessionEligible"] else 1
        finally:
            transport.close()
    except Exception as exc:
        print(f"Mini-Benchmark nicht abgeschlossen: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
