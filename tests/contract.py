"""Prüft jede Antwort gegen contract/session-chronik-api.yaml.

Streng: Jeder Statuscode muss in der YAML stehen, und jedes in der YAML beschriebene
Feld muss in der Antwort vorkommen (fängt Tippfehler bei Feldnamen ab).
"""
import copy
import json
import re
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

SPEC_PATH = Path(__file__).resolve().parent.parent / "contract" / "session-chronik-api.yaml"
PREFIX = "/api/v1"
URN = "urn:session-chronik"


# Felder, die laut YAML je nach Betrachter/Fall WEGGELASSEN werden (nicht null):
# characterBackstory und gmNotes für Unbefugte, bei Anwesenden genau eines von memberId/guestName,
# bei Belegstellen start (Session) oder page (Unterlage).
# user beim Tausch (0.4.0) nur bei status ok – sonst fehlt es (die YAML erlaubt dort kein null).
OPTIONAL_FIELDS = {"characterBackstory", "gmNotes", "hiddenFromMemberIds", "memberId", "guestName", "start", "page",
                   "review", "hotwords",  # 0.4.6: nur für die SL, für Spieler weggelassen
                   "gmNotices", "originCharacterId", "originEntryId", "originVersion",  # 0.4.7: nur SL (und Urheberin)
                   "details",  # 0.4.7: Error.details nur bei manchen Fehlercodes
                   "characterId", "characterVersion",  # 0.4.8: nur für die Person selbst und die SL
                   "missingChunks",  # 0.4.8: ImportStatus nur bei uploading
                   "formerHolderMemberId",  # 0.4.9: nur für die SL
                   "assignedMemberId",  # 0.4.10: erst nach der Bestätigung der Stimmen
                   "user"}

# Statuscodes, die in der YAML fehlen, aber fachlich nötig sind – für den nächsten Änderungswunsch notiert.
BEKANNTE_LUECKEN: set[tuple[str, str, int]] = set()


def _require_all(node):
    if isinstance(node, dict):
        if isinstance(node.get("properties"), dict):
            pflicht = set(node["properties"].keys()) - OPTIONAL_FIELDS
            node["required"] = sorted(set(node.get("required", [])) | pflicht)
        for v in node.values():
            _require_all(v)
    elif isinstance(node, list):
        for v in node:
            _require_all(v)


def _escape(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


class Contract:
    def __init__(self):
        raw = yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8"))
        # YAML 1.1 liest [yes, maybe, no] als Wahrheitswerte – gemeint sind die Texte (die YAML quotet sie inzwischen, ältere Fassungen nicht)
        raw["components"]["schemas"]["VoteAnswer"]["enum"] = ["yes", "maybe", "no"]
        self.spec = copy.deepcopy(raw)
        _require_all(self.spec)
        self.registry = Registry().with_resource(URN, Resource.from_contents(self.spec, default_specification=DRAFT202012))
        self.templates = []
        for tpl in self.spec["paths"]:
            rx = "^" + re.sub(r"\{[^}]+\}", r"[^/]+", tpl) + "$"
            self.templates.append((re.compile(rx), tpl))
        # Konkrete Pfade vor Platzhaltern (z. B. /campaigns/join vor /campaigns/{campaignId})
        self.templates.sort(key=lambda t: t[1].count("{"))

    def _resolve(self, node: dict) -> dict:
        while "$ref" in node:
            ptr = node["$ref"].lstrip("#/").split("/")
            node = self.spec
            for p in ptr:
                node = node[p.replace("~1", "/").replace("~0", "~")]
        return node

    def check(self, method: str, url_path: str, status: int, content_type: str | None, body: bytes) -> None:
        if method.upper() == "OPTIONS" or not url_path.startswith(PREFIX):
            return
        path = url_path[len(PREFIX):]
        if path == "/health":
            return
        tpl = next((t for rx, t in self.templates if rx.match(path)), None)
        assert tpl is not None, f"Pfad nicht in der YAML: {path}"
        op = self.spec["paths"][tpl].get(method.lower())
        assert op is not None, f"Methode {method} für {tpl} nicht in der YAML"
        responses = op["responses"]
        key = str(status)
        if (method.upper(), tpl, status) in BEKANNTE_LUECKEN:
            return
        if status == 426 and body and json.loads(body).get("code") == "app_outdated":
            return  # 0.3.9: kann jede API-Anfrage treffen (außer /info, /health)
        if status == 409 and body and json.loads(body).get("code") == "feature_unavailable":
            # Mit der App vereinbart: noch nicht fertige Funktionen antworten überall so.
            return
        assert key in responses, f"{method} {tpl}: Statuscode {status} nicht in der YAML (erlaubt: {sorted(responses)})"
        resp = self._resolve(responses[key])
        content = resp.get("content")
        if not content:
            assert not body, f"{method} {tpl} {status}: YAML erwartet keinen Inhalt, bekam {body[:200]!r}"
            return
        if "application/json" not in content:
            return
        assert content_type and content_type.startswith("application/json"), (
            f"{method} {tpl} {status}: JSON erwartet, bekam {content_type}"
        )
        pointer = "/".join(
            ["#", "paths", _escape(tpl), method.lower(), "responses", key]
        )
        if "$ref" in responses[key]:
            pointer = responses[key]["$ref"]
        pointer += "/content/application~1json/schema"
        validator = Draft202012Validator(
            {"$ref": URN + pointer}, registry=self.registry, format_checker=FormatChecker()
        )
        data = json.loads(body)
        errs = sorted(validator.iter_errors(data), key=lambda e: list(e.absolute_path))
        if errs:
            e = errs[0]
            raise AssertionError(
                f"{method} {tpl} {status}: Antwort passt nicht zur YAML bei "
                f"{'/'.join(map(str, e.absolute_path)) or '(Wurzel)'}: {e.message}"
            )


CONTRACT = Contract()
