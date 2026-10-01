"""Kommandozeile:  uv run chronik --help"""
import json
from datetime import timedelta
from pathlib import Path

import typer
from sqlalchemy import select

from app.db import run_migrations, session_factory, utcnow
from app.models import AuthMethod, Campaign, Entry, EntryMention, GameSession, Member, Proposal, Recap, User
from app.security import hash_password
from app.services import build_attendees, default_organization, ensure_org_member, random_cover, set_recording_consent
from app import schemas

app = typer.Typer(help="Verwaltung des Session-Chronik-Servers", no_args_is_help=True)


def _db():
    run_migrations()
    return session_factory()()


def _neues_konto(db, username: str, display_name: str, password: str, admin: bool = False) -> User:
    """Konto + Anmeldeart „Passwort“ + Mitgliedschaft in der (Standard-)Organisation."""
    u = User(username=username, display_name=display_name)
    db.add(u)
    db.flush()
    db.add(AuthMethod(user_id=u.id, kind="password", secret=hash_password(password)))
    org = default_organization(db)
    ensure_org_member(db, org.id, u, role="admin" if admin else "member")
    return u


def _ask_password() -> str:
    pw = typer.prompt("Passwort", hide_input=True, confirmation_prompt="Passwort wiederholen")
    if len(pw) < 8:
        typer.echo("Das Passwort muss mindestens 8 Zeichen haben.", err=True)
        raise typer.Exit(1)
    return pw


@app.command("create-user")
def create_user(
    username: str = typer.Option(..., "--username", "-u", help="Anmeldename (ohne Leerzeichen)"),
    display_name: str = typer.Option(..., "--name", "-n", help="Anzeigename, z. B. 'Anna'"),
    admin: bool = typer.Option(False, "--admin", help="Als Verwalter der Organisation (Verein) eintragen"),
):
    """Neues Benutzerkonto anlegen (Passwort wird abgefragt)."""
    uname = username.strip().lower()
    if not uname or " " in uname:
        typer.echo("Ungültiger Benutzername.", err=True)
        raise typer.Exit(1)
    db = _db()
    if db.scalar(select(User).where(User.username == uname)):
        typer.echo(f"Den Benutzer '{uname}' gibt es schon.", err=True)
        raise typer.Exit(1)
    pw = _ask_password()
    _neues_konto(db, uname, display_name.strip(), pw, admin=admin)
    db.commit()
    typer.echo(f"Benutzer '{uname}' angelegt.")


@app.command("reset-password")
def reset_password(username: str = typer.Option(..., "--username", "-u")):
    """Passwort neu setzen. Alle bestehenden Anmeldungen dieses Benutzers werden ungültig."""
    db = _db()
    user = db.scalar(select(User).where(User.username == username.strip().lower()))
    if user is None:
        typer.echo("Benutzer nicht gefunden.", err=True)
        raise typer.Exit(1)
    methode = db.scalar(select(AuthMethod).where(AuthMethod.user_id == user.id, AuthMethod.kind == "password"))
    if methode is None:
        methode = AuthMethod(user_id=user.id, kind="password", secret="")
        db.add(methode)
    methode.secret = hash_password(_ask_password())
    user.token_version += 1
    db.commit()
    typer.echo("Passwort geändert, alte Anmeldungen sind abgemeldet.")


@app.command("list-users")
def list_users():
    """Alle Benutzer anzeigen."""
    db = _db()
    for u in db.scalars(select(User).order_by(User.username)):
        typer.echo(f"{u.username:20} {u.display_name}")


@app.command("demo-data")
def demo_data(password: str = typer.Option("chronik-demo", help="Passwort für beide Demo-Konten")):
    """Testdaten anlegen: Konten 'sl' und 'spieler', eine Kampagne mit geheimen und öffentlichen Inhalten."""
    db = _db()
    if db.scalar(select(User).where(User.username.in_(["sl", "spieler"]))):
        typer.echo("Demo-Konten existieren schon.", err=True)
        raise typer.Exit(1)
    gm_user = _neues_konto(db, "sl", "Spielleitung (Demo)", password, admin=True)
    pl_user = _neues_konto(db, "spieler", "Spielerin (Demo)", password)
    c = Campaign(title="Die Chroniken von Rabenfels", description="Demo-Kampagne zum Testen des Spoilerschutzes.",
                 organization_id=default_organization(db).id, language="de", system="dsa", cover_preset=random_cover(),
                 world_info="Rabenfels ist eine kleine Stadt am Rand des Finsterwalds.\n\n"
                            "Tischregel: Handys bleiben während der Szenen in der Tasche.")
    db.add(c)
    db.flush()
    gm = Member(campaign_id=c.id, user_id=gm_user.id, role="gm")
    pl = Member(campaign_id=c.id, user_id=pl_user.id, role="player", character_name="Mira Dornbusch",
                character_summary="Junge Kräuterkundige aus dem Finsterwald.",
                character_backstory="GEHEIM: Mira ist die verschollene Tochter des Bürgermeisters.")
    db.add_all([gm, pl])
    db.flush()
    set_recording_consent(db, pl, True)  # die Spielerin hat in ihrer App zugestimmt

    now = utcnow().replace(microsecond=0)
    att = [schemas.AttendeeIn(member_id=gm.id, consent=True, consent_source="on_site"),
           schemas.AttendeeIn(member_id=pl.id, consent=True, consent_source="app")]
    s1 = GameSession(campaign_id=c.id, number=1, title="Ankunft in Rabenfels", played_at=now - timedelta(days=14),
                     state="published", published_at=now - timedelta(days=13), source="table", duration_seconds=4 * 3600,
                     audio_deleted_at=now - timedelta(days=14))
    s1.attendees = build_attendees(db, c.id, att)
    s2 = GameSession(campaign_id=c.id, number=2, title="Der Keller unter dem Wirtshaus", played_at=now - timedelta(days=1),
                     state="awaiting_review", source="table", duration_seconds=3 * 3600,
                     audio_deleted_at=now - timedelta(days=1))
    s2.attendees = build_attendees(db, c.id, att)
    db.add_all([s1, s2])
    db.flush()

    hilde = Entry(campaign_id=c.id, type="npc", name="Hilde Krugmann",
                  summary="Wirtin des „Grauen Raben“, hilfsbereit, kennt jeden Klatsch.", visibility="public",
                  gm_notes="Schwester des Grauen Fürsten – spioniert für ihn.")
    fuerst = Entry(campaign_id=c.id, type="npc", name="Der Graue Fürst",
                   summary="Hildes Bruder, zieht im Hintergrund die Fäden.", visibility="gm_only")
    keller = Entry(campaign_id=c.id, type="location", name="Keller unter dem Grauen Raben",
                   summary="Verborgener Gang Richtung Burg.", visibility="public")
    quest = Entry(campaign_id=c.id, type="quest", name="Das verschwundene Siegel",
                  summary="Der Bürgermeister sucht sein Amtssiegel.", status="active", visibility="public")
    amulett = Entry(campaign_id=c.id, type="item", name="Rabenamulett",
                    summary="Schwarzes Amulett, wird nachts warm.", holder_member_id=pl.id, visibility="public")
    for e in (hilde, fuerst, keller, quest, amulett):
        if e.visibility == "public":
            e.public_changed_at = now
    db.add_all([hilde, fuerst, keller, quest, amulett])
    db.flush()
    db.add_all([
        EntryMention(entry_id=hilde.id, session_id=s1.id, note="Empfängt die Gruppe im Wirtshaus."),
        EntryMention(entry_id=quest.id, session_id=s1.id, note="Auftrag angenommen."),
        EntryMention(entry_id=hilde.id, session_id=s2.id, note="Verrät sich beim Keller (unveröffentlicht)."),
        EntryMention(entry_id=keller.id, session_id=s2.id, note="Entdeckt (unveröffentlicht)."),
        EntryMention(entry_id=fuerst.id, session_id=s2.id, note="Erster Auftritt im Schatten."),
    ])
    # Recaps und Vorschläge (sonst liefert der Recap von Kapitel 1 für Spieler 404)
    db.add_all([
        Recap(session_id=s1.id, title="Ankunft in Rabenfels", model="demo",
              text="Nebel hing über den Dächern, als die Gefährten Rabenfels erreichten.\n\n"
                   "Im „Grauen Raben“ nahm Hilde Krugmann sie auf, und der Bürgermeister bat um Hilfe: "
                   "Sein Amtssiegel ist verschwunden.",
              open_threads=json.dumps(["Wer hat das Siegel gestohlen?"], ensure_ascii=False)),
        Recap(session_id=s2.id, title="Der Keller unter dem Wirtshaus", model="demo",
              text="Hinter einem Weinregal fand Mira einen verborgenen Gang.\n\n"
                   "Eine Stimme im Dunkeln sprach vom „Fürsten“ – dann erlosch die Laterne.",
              open_threads=json.dumps(["Wohin führt der Gang?", "Wer ist der Fürst?"], ensure_ascii=False)),
        Proposal(campaign_id=c.id, session_id=s2.id, position=0, entry_type="npc", action="create",
                 title="Der Kellermeister", detail="Alter Mann mit Laterne, bewacht den Gang.",
                 suggested_visibility="public", visibility_reason="Die Gruppe hat mit ihm gesprochen.",
                 confidence=0.85, evidence=json.dumps([{"start": 1520.0, "quote": "Ich bin hier nur der Kellermeister."}],
                                                     ensure_ascii=False)),
        Proposal(campaign_id=c.id, session_id=s2.id, position=1, entry_type="npc", action="reveal",
                 target_entry_id=fuerst.id, title="Der Graue Fürst",
                 detail="Eine Stimme im Keller sprach von einem „Fürsten“, der im Hintergrund wartet.",
                 gm_notes="Hildes Bruder, zieht im Hintergrund die Fäden.", suggested_visibility="public",
                 visibility_reason="Die Gruppe hat den Namen im Keller gehört.", confidence=0.7,
                 evidence=json.dumps([{"start": 9810.0, "quote": "Der Fürst wird nicht erfreut sein."}],
                                     ensure_ascii=False)),
        Proposal(campaign_id=c.id, session_id=s2.id, position=2, entry_type="location", action="update",
                 target_entry_id=keller.id, title="Keller unter dem Grauen Raben",
                 detail="Hinter dem Weinregal beginnt ein Gang Richtung Burg.", suggested_visibility="public",
                 confidence=0.8, evidence=json.dumps([{"start": 4210.0, "quote": "Da ist ein Luftzug hinter dem Regal!"}],
                                                     ensure_ascii=False)),
        Proposal(campaign_id=c.id, session_id=s2.id, position=3, entry_type="item", action="create",
                 title="Goldene Gummiente", detail="Angeblich das mächtigste Artefakt Aventuriens.",
                 suggested_visibility="gm_only", confidence=0.2, flags=json.dumps(["joke_suspected", "low_confidence"]),
                 evidence=json.dumps([{"start": 6000.0, "quote": "Ich kaufe die goldene Gummiente!"}],
                                     ensure_ascii=False)),
    ])
    db.commit()
    typer.echo(
        "Demo angelegt.\n"
        f"  SL:      sl / {password}\n"
        f"  Spieler: spieler / {password}\n"
        "Spieler sollte sehen: Kapitel 1, vier öffentliche Einträge (ohne 'Der Graue Fürst'),\n"
        "bei Hilde nur die Erwähnung aus Kapitel 1 und KEINE SL-Notiz, 'Keller' ohne Erwähnungen.\n"
        "Den geheimen Hintergrund von Mira sehen nur 'spieler' selbst und die SL.\n"
        "Kapitel 2 wartet auf Prüfung: Recap und vier Vorschläge (neu, aufdecken, ergänzen, Scherz)."
    )


@app.command("probelauf")
def probelauf(
    datei: str = typer.Argument(..., help="Audiodatei (mp3, m4a, wav, …), z. B. aufnahme.mp3"),
    ab: float = typer.Option(0, "--ab", help="Start in Minuten (z. B. 10 = ab Minute 10)"),
    dauer: float = typer.Option(None, "--dauer", help="Nur so viele Minuten verarbeiten (Standard: alles)"),
    tisch: bool = typer.Option(False, "--tisch", help="Klang eines Handys mitten am Spieltisch simulieren"),
    sprecher: int = typer.Option(None, "--sprecher", help="Genaue Anzahl Sprecher, falls bekannt"),
    min_sprecher: int = typer.Option(None, "--min-sprecher"),
    max_sprecher: int = typer.Option(None, "--max-sprecher"),
    namen: str = typer.Option(None, "--namen", help="Eigennamen als Schreibhilfe, kommagetrennt: \"Borbarad,Gareth\""),
    ohne_sprecher: bool = typer.Option(False, "--ohne-sprecher", help="Nur transkribieren, keine Sprechertrennung"),
    sprache: str = typer.Option("de", "--sprache"),
    modell: str = typer.Option("large-v3", "--modell", help="Whisper-Modell (large-v3, large-v3-turbo, medium …)"),
    genauigkeit: str = typer.Option("int8_float16", "--genauigkeit", help="int8_float16 (spart Speicher) oder float16"),
    batch: int = typer.Option(8, "--batch", help="Kleiner = weniger Grafikspeicher, größer = schneller"),
):
    """Transkriptions-Probelauf mit WhisperX und pyannote (ohne App, ohne Datenbank)."""
    from pathlib import Path

    from app.config import get_settings
    from app.probelauf import ProbelaufFehler, ausfuehren

    s = get_settings()
    try:
        ordner = ausfuehren(
            Path(datei).expanduser(), sprache=sprache, modell=modell, genauigkeit=genauigkeit, batch=batch,
            ab_min=ab, dauer_min=dauer, tisch=tisch, sprecher=sprecher, min_sprecher=min_sprecher,
            max_sprecher=max_sprecher, namen=namen, ohne_sprecher=ohne_sprecher, hf_token=s.hf_token,
            ausgabe_basis=s.data_dir / "probelauf",
        )
    except ProbelaufFehler as e:
        typer.echo(f"\n✗ {e}", err=True)
        raise typer.Exit(1)
    typer.echo(f"\nFertig. Ergebnisse in: {ordner}")
    typer.echo("  bericht.md      – Laufzeiten, Stimmen, Vorstellungsrunde")
    typer.echo("  transkript.txt  – lesbares Transkript")
    try:
        import subprocess

        win = subprocess.run(["wslpath", "-w", str(ordner)], capture_output=True, text=True).stdout.strip()
        if win:
            typer.echo(f'Im Windows-Explorer öffnen:  explorer.exe "{win}"')
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------- Worker
token_app = typer.Typer(help="Zugangsschlüssel (Tokens) für Worker verwalten", no_args_is_help=True)
app.add_typer(token_app, name="worker-token")


@token_app.command("create")
def worker_token_create(
    name: str = typer.Option(..., "--name", help="z. B. heim-pc"),
    capabilities: str = typer.Option("asr,llm", help="Fähigkeiten, kommagetrennt (asr, llm)"),
):
    """Neuen Worker anmelden. Der Token wird nur EINMAL angezeigt."""
    import secrets

    from app.models import Worker
    from app.routers.worker import token_hash

    db = _db()
    geheim = secrets.token_urlsafe(32)
    w = Worker(name=name.strip(), token_hash=token_hash(geheim), capabilities=capabilities.replace(" ", ""))
    db.add(w)
    db.commit()
    typer.echo(f"Worker '{w.name}' angelegt. Token (jetzt kopieren, wird nicht wieder angezeigt):\n")
    typer.echo(f"  wk.{w.id}.{geheim}\n")
    typer.echo("Auf dem Worker in die .env eintragen:  WORKER_TOKEN=<Token>")


@token_app.command("list")
def worker_token_list():
    """Alle Worker mit letztem Lebenszeichen."""
    from app.models import Worker

    db = _db()
    for w in db.scalars(select(Worker).order_by(Worker.created_at)):
        status = "gesperrt" if w.revoked_at else "aktiv"
        zuletzt = w.last_seen_at.strftime("%d.%m.%Y %H:%M UTC") if w.last_seen_at else "noch nie"
        typer.echo(f"{w.name:20} {status:9} {w.capabilities:10} zuletzt gesehen: {zuletzt}")


@token_app.command("revoke")
def worker_token_revoke(name: str = typer.Option(..., "--name")):
    """Token sperren (z. B. wenn ein Computer abhandengekommen ist)."""
    from app.models import Worker

    db = _db()
    w = db.scalar(select(Worker).where(Worker.name == name, Worker.revoked_at.is_(None)))
    if w is None:
        typer.echo("Kein aktiver Worker mit diesem Namen.", err=True)
        raise typer.Exit(1)
    w.revoked_at = utcnow()
    db.commit()
    typer.echo(f"Worker '{name}' gesperrt.")


def _systemzertifikate() -> None:
    """Zertifikate aus dem Speicher des Betriebssystems nutzen (Windows, macOS). Virenscanner mit HTTPS-Prüfung
    (Kaspersky, ESET, Avast …) und Firmen-Proxys schieben eigene Stammzertifikate dazwischen; die kennt das
    mitgelieferte certifi-Bündel nicht, und der Server wäre „nicht erreichbar“, obwohl der Browser ihn öffnet."""
    import logging

    try:
        import truststore
    except ImportError:
        return
    try:
        truststore.inject_into_ssl()
    except Exception as e:  # noqa: BLE001 – dann eben nur certifi
        logging.getLogger("worker").warning("Systemzertifikate nicht nutzbar: %s", e)


@app.command("worker")
def worker(
    attrappe: bool = typer.Option(False, "--testmodus", "--attrappe", help="Ohne KI: Platzhaltertext statt Transkription (zum Testen ohne Grafikkarte)"),
    server: str = typer.Option(None, "--server", help="Adresse des Servers (Standard: WORKER_SERVER_URL)"),
    token: str = typer.Option(None, "--token", help="Worker-Token (Standard: WORKER_TOKEN aus der .env)"),
    unsicher: bool = typer.Option(False, "--unsicher", help="HTTP auch außerhalb von localhost erlauben (nur Heimnetz-Tests)"),
    einmal: bool = typer.Option(False, "--einmal", help="Nur einen Auftrag bearbeiten, dann beenden"),
    selbsttest: str = typer.Option(None, "--selbsttest", help="Eine Audiodatei ohne Server verarbeiten (Prüfung der Einrichtung)"),
    sprecher: int = typer.Option(2, "--sprecher", help="Nur mit --selbsttest: erwartete Anzahl Personen"),
    namen: str = typer.Option("", "--namen", help="Nur mit --selbsttest: Namenshilfe, kommagetrennt"),
    koppeln: str = typer.Option(None, "--koppeln", help="Kopplungscode aus der Verwaltung (Transkription → Worker "
                                                          "koppeln); trägt Schlüssel und Adresse selbst in die .env ein"),
    name: str = typer.Option(None, "--name", help="Nur mit --koppeln: Name dieses Workers (Standard: Gerätename)"),
    app_modus: bool = typer.Option(False, "--app", hidden=True,
                                   help="Für die Worker-App: Ereignisse als JSON-Zeilen, Steuerung über stdin"),
    automatisch: bool = typer.Option(False, "--automatisch", help="Modell, Stapelgröße und Geräte selbst nach dem "
                                     "Grafikspeicher wählen (GPU_MEMORY_LIMIT_MB, WORKER_MODELL, WHISPER_DEVICE=cpu)"),
):
    """Worker starten: holt Aufträge vom Server und verarbeitet sie."""
    import logging

    from app.worker_app import Anbindung

    anbindung = Anbindung() if app_modus else None
    melden = anbindung.melden if anbindung else (lambda ereignis, **daten: None)

    from app.config import get_settings
    from app.worker_prozess import WorkerProzess, pruefe_adresse, verarbeite_attrappe

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # nicht jede Anfrage protokollieren
    _systemzertifikate()
    s = get_settings()
    server = server or s.worker_server_url
    token = token or s.worker_token or _token_aus_datei(s.worker_token_file)
    if koppeln:
        token = _koppeln(server, koppeln, name, unsicher)
    if not token and not selbsttest:
        typer.echo("Kein Worker-Token. Anlegen mit: uv run chronik worker-token create --name heim-pc", err=True)
        raise typer.Exit(1)
    pruefe_adresse(server, unsicher)
    neustart_noetig = None
    if attrappe:
        verarbeite, info = verarbeite_attrappe, {"modus": "attrappe"}
    else:
        import warnings

        from app.transkription import EinrichtungsFehler, WhisperXMotor, verarbeiter

        # Harmlose Hinweise der KI-Bibliotheken ausblenden (Lightning-Checkpoint, TF32, std())
        warnings.filterwarnings("ignore", module=r"pyannote\..*")
        warnings.filterwarnings("ignore", message=r".*TensorFloat-32.*")
        for name in ("lightning", "lightning.pytorch", "pytorch_lightning", "lightning_fabric"):
            logging.getLogger(name).setLevel(logging.ERROR)

        from app.modelle import ServerQuelle

        melden("pruefe")
        hf_token = s.hf_token or (_hf_vom_server(server, token) if token else None)
        quelle = ServerQuelle(server, token) if token else None  # Sprechermodell vom Server
        if automatisch:
            from app.arbeitsweise import grafikspeicher_mb, profil

            # Eingebauter Worker: Werte aus der Verwaltung gehen vor denen aus der Umgebung (.env/Compose)
            eingebaut = (_config_vom_server(server, token).get("eingebaut") or {}) if token else {}
            p = profil(grafikspeicher_mb(), eingebaut.get("vramMb", s.gpu_memory_limit_mb),
                       eingebaut.get("modell", s.worker_modell),
                       prozessor=eingebaut.get("prozessor", s.whisper_device == "cpu"))
            threads = eingebaut.get("threads", s.cpu_threads)
            typer.echo(f"Arbeitsweise: {p['stufe']} – {p['modell']}, Stapel {p['batch']}, Geräte {p['geraet']}/"
                       f"{p['ausrichten']}/{p['sprecher']}")
            motor = WhisperXMotor(p["modell"], p["genauigkeit"], p["batch"], hf_token, quelle, geraet=p["geraet"],
                                  geraet_ausrichten=p["ausrichten"], geraet_sprecher=p["sprecher"],
                                  grenze_mb=p["grenzeMb"], threads=threads)

            def neustart_noetig() -> bool:
                aktuell = _config_vom_server(server, token, fehler_werfen=True).get("eingebaut") or {}
                return aktuell != eingebaut
        else:
            motor = WhisperXMotor(s.whisper_model, s.whisper_compute_type, s.whisper_batch, hf_token, quelle,
                                  geraet=s.whisper_device, geraet_ausrichten=s.align_device,
                                  geraet_sprecher=s.diarize_device, grenze_mb=s.gpu_memory_limit_mb,
                                  threads=s.cpu_threads)
        try:
            info = motor.pruefen()
        except EinrichtungsFehler as e:
            melden("fehler", message=str(e))
            typer.echo(f"Worker kann nicht starten: {e}", err=True)
            raise typer.Exit(1)
        grenze = f", höchstens {info['grenzeMb']} MB" if info.get("grenzeMb") else ""
        typer.echo(f"Gerät: {info['gpu']} ({info['geraete']}), Modell {info['modell']} ({info['genauigkeit']}){grenze}")
        typer.echo("Hinweis: Beim ersten Auftrag werden die Modelle geladen (einige GB, einmalig).")
        verarbeite = verarbeiter(motor)
    if selbsttest:
        _selbsttest(Path(selbsttest), verarbeite, s.worker_work_dir.expanduser().resolve(), sprecher, namen)
        return
    from app.worker_prozess import lokales_sprachmodell

    zusammenfassen, llm = lokales_sprachmodell(s.worker_llm_url)
    if llm:
        info = {**info, "llm": llm}
        typer.echo(f"Sprachmodell: {llm} – übernimmt auch Zusammenfassungen, wenn die Verwaltung „Lokales Modell“ "
                   "eingestellt hat.")
    knecht = WorkerProzess(server, token, s.worker_work_dir.expanduser().resolve(), verarbeite, info=info,
                          zusammenfassen=zusammenfassen, melden=melden,
                          neustart_noetig=neustart_noetig if automatisch and token else None)
    melden("bereit", gpu=info.get("gpu"), modell=info.get("modell"), vramMb=info.get("vramMb"), llm=llm,
           testmodus=attrappe, geraete=info.get("geraete"), grenzeMb=info.get("grenzeMb"))
    if anbindung:
        anbindung.steuern(knecht)
    try:
        if einmal:
            knecht.aufraeumen()
            while not knecht.einen_auftrag():
                pass
        else:
            knecht.laufen()
    except KeyboardInterrupt:
        typer.echo("\nWorker beendet.")
    except SystemExit as e:
        if e.code not in (None, 0):
            melden("fehler", message=str(e.code))
        raise
    finally:
        melden("beendet")


def _token_aus_datei(datei: Path | None, warten_s: float = 60.0) -> str | None:
    """Eingebauter Worker: Schlüssel aus der Datei, die der Server beim Start schreibt (wartet kurz darauf)."""
    import time

    if datei is None:
        return None
    ende = time.monotonic() + warten_s
    while True:
        try:
            wert = datei.read_text(encoding="utf-8").strip()
            if wert:
                return wert
        except OSError:
            pass
        if time.monotonic() >= ende:
            return None
        time.sleep(2)


def _env_setzen(werte: dict[str, str], datei: Path = Path(".env")) -> None:
    """Schlüssel in der .env setzen oder ersetzen, alles andere bleibt, wie es ist."""
    zeilen = datei.read_text(encoding="utf-8").splitlines() if datei.exists() else []
    offen = dict(werte)
    for i, z in enumerate(zeilen):
        schluessel = z.split("=", 1)[0].strip().lstrip("# ").strip()
        if schluessel in offen and ("=" in z):
            zeilen[i] = f"{schluessel}={offen.pop(schluessel)}"
    zeilen += [f"{k}={v}" for k, v in offen.items()]
    datei.write_text("\n".join(zeilen) + "\n", encoding="utf-8")


def _koppeln(server: str, code: str, name: str | None, unsicher: bool) -> str:
    import socket

    import httpx

    from app.config import get_settings
    from app.worker_prozess import pruefe_adresse

    pruefe_adresse(server, unsicher)
    try:
        r = httpx.post(f"{server.rstrip('/')}/worker/v1/pair", json={"code": code, "name": name or socket.gethostname()},
                       timeout=20)
    except httpx.HTTPError as e:
        typer.echo(f"Server {server} nicht erreichbar ({type(e).__name__}). Adresse mit --server angeben.", err=True)
        raise typer.Exit(1)
    if r.status_code != 201:
        typer.echo((r.json().get("message") if r.headers.get("content-type", "").startswith("application/json")
                    else r.text)[:300], err=True)
        raise typer.Exit(1)
    d = r.json()
    _env_setzen({"WORKER_TOKEN": d["token"], "WORKER_SERVER_URL": server})
    get_settings.cache_clear()
    typer.echo(f"Gekoppelt als „{d['name']}“. Schlüssel und Adresse stehen jetzt in der .env – beim nächsten Mal "
               "reicht „uv run chronik worker“.")
    return d["token"]


def _config_vom_server(server: str, token: str, fehler_werfen: bool = False) -> dict:
    """Einstellungen, die der Server für diesen Worker verwaltet (/worker/v1/config)."""
    import httpx

    try:
        r = httpx.get(f"{server.rstrip('/')}/worker/v1/config", headers={"Authorization": f"Bearer {token}"},
                      timeout=15)
        r.raise_for_status()
        return r.json()
    except (httpx.HTTPError, ValueError):
        if fehler_werfen:
            raise httpx.HTTPError("Server nicht erreichbar")
        return {}


def _hf_vom_server(server: str, token: str) -> str | None:
    """Hugging-Face-Zugang, den der Betreiber in der Verwaltung hinterlegt hat."""
    import httpx

    try:
        r = httpx.get(f"{server.rstrip('/')}/worker/v1/config", headers={"Authorization": f"Bearer {token}"},
                      timeout=15)
        return r.json().get("hfToken") if r.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None


def _selbsttest(datei: Path, verarbeite, arbeitsordner: Path, sprecher: int, namen: str) -> None:
    """Gleicher Weg wie ein echter Auftrag, nur ohne Server: zeigt Stimmen, Tempo und die ersten Zeilen."""
    import shutil
    import time

    from app.audio import AudioFehler

    if not datei.is_file():
        typer.echo(f"Datei nicht gefunden: {datei}", err=True)
        raise typer.Exit(1)
    arbeit = arbeitsordner / "selbsttest"
    shutil.rmtree(arbeit, ignore_errors=True)
    arbeit.mkdir(parents=True)
    auftrag = {"session": {"language": "de", "source": "table", "expectedSpeakers": sprecher,
                           "hotwords": [n.strip() for n in namen.split(",") if n.strip()]},
               "files": [{"fileId": "selbsttest", "position": 0}]}
    letzte = [-1]

    def fortschritt(p: float) -> None:
        pct = int(p * 100)
        if pct // 10 > letzte[0] // 10:
            typer.echo(f"  {pct} %")
            letzte[0] = pct

    t0 = time.monotonic()
    try:
        erg = verarbeite(auftrag, [datei], arbeit, fortschritt)
    except AudioFehler as e:
        typer.echo(f"Fehlgeschlagen ({e.code}): {e}", err=True)
        raise typer.Exit(1)
    finally:
        shutil.rmtree(arbeit, ignore_errors=True)
    dauer = time.monotonic() - t0
    typer.echo(f"\nAudio {erg['audioSeconds'] / 60:.1f} min in {dauer / 60:.1f} min verarbeitet "
               f"(Grafikspeicher-Spitze: {erg.get('peakVramMb') or '–'} MB)")
    typer.echo(f"{len(erg['speakers'])} Stimme(n):")
    for sp in sorted(erg["speakers"], key=lambda x: -x["speakingSeconds"]):
        typer.echo(f"  {sp['label']}: {sp['speakingSeconds'] / 60:.1f} min, Abdruck: "
                   f"{'ja' if sp.get('embedding') else 'nein'} – „{sp['sampleText'][:70]}“")
    typer.echo("Erste Zeilen:")
    for seg in erg["segments"][:8]:
        typer.echo(f"  [{int(seg['start']) // 60:02d}:{int(seg['start']) % 60:02d}] {seg['speaker'] or '?'}: {seg['text'][:90]}")


@app.command("admin")
def admin(
    username: str = typer.Option(..., "--username", "-u"),
    entziehen: bool = typer.Option(False, "--entziehen", help="Verwalter-Recht wieder entziehen"),
):
    """Konto zum Verwalter machen (Zugang zur Weboberfläche /verwaltung)."""
    from app.models import OrgMember

    db = _db()
    user = db.scalar(select(User).where(User.username == username.strip().lower()))
    if user is None:
        typer.echo(f"Benutzer '{username}' nicht gefunden.", err=True)
        raise typer.Exit(1)
    org = default_organization(db)
    ensure_org_member(db, org.id, user)
    om = db.get(OrgMember, (org.id, user.id))
    om.role = "member" if entziehen else "admin"
    db.commit()
    typer.echo(f"'{user.username}' ist jetzt {'kein ' if entziehen else ''}Verwalter von „{org.name}“.")


@app.command("modelle")
def modelle_zeigen(
    laden: bool = typer.Option(False, "--laden", help="Fehlende Modelle jetzt laden (sonst beim Start des Workers)"),
):
    """Welche Modelle in welcher Fassung auf diesem Computer liegen."""
    from app import modelle
    from app.config import get_settings

    s = get_settings()
    zeilen = [("Whisper", modelle.whisper_repo(s.whisper_model)), ("Sprechermodell", modelle.SPRECHERMODELL)]
    gemerkt = modelle.gemerkt()
    for titel, repo in zeilen:
        if laden:
            try:
                b = modelle.bereitstellen(repo, s.hf_token)
            except modelle.ModellFehler as e:
                typer.echo(f"{titel}: {repo}\n  Fehler: {e}", err=True)
                raise typer.Exit(1)
            typer.echo(f"{titel}: {repo}\n  Fassung {b.fassung or 'eigener Ordner'} ({b.quelle})\n  Ordner {b.pfad}")
            continue
        fest, merk = modelle.FASSUNGEN.get(repo), gemerkt.get(repo)
        if fest:
            stand = f"festgelegt {fest}" + ("" if merk == fest else " – noch nicht geladen")
        elif merk:
            stand = f"gemerkt {merk}"
        else:
            stand = "noch nicht geladen (wird beim Start des Workers geholt und gemerkt)"
        typer.echo(f"{titel}: {repo}\n  {stand}")
    typer.echo("Ausrichtung und Spracherkennung: fest mit den Paketversionen (uv.lock).")


@app.command("modell-holen")
def modell_holen():
    """Sprechermodell auf den Server laden (für alle Worker). Sonst erledigt das die Wartung von selbst."""
    from app import modellablage

    with _db() as db:
        try:
            stand = modellablage.holen(db)
        except modellablage.AblageFehler as e:
            typer.echo(f"Nicht geladen: {e}", err=True)
            raise typer.Exit(1)
    typer.echo(f"{stand.repo} in Fassung {stand.fassung} liegt auf dem Server ({stand.groesse / 1024 ** 2:.1f} MB, "
               f"Quelle {stand.quelle}).")


@app.command("modell-spiegel")
def modell_spiegel(ziel: Path = typer.Argument(..., help="Ordner, der später auf einem Webserver liegt")):
    """Für das Taleward-Projekt: Spiegel des Sprechermodells erstellen (mit Namensnennung nach CC-BY-4.0).
    Die ausgegebene Prüfsumme gehört in SPIEGEL in app/modellablage.py."""
    from app import modellablage

    with _db() as db:
        try:
            ordner, summe = modellablage.spiegel_erstellen(db, ziel.expanduser().resolve())
        except modellablage.AblageFehler as e:
            typer.echo(str(e), err=True)
            raise typer.Exit(1)
        stand = modellablage.vorhanden(db)
    typer.echo(f"Spiegel liegt in {ordner}")
    typer.echo(f'In app/modellablage.py eintragen:  SPIEGEL = {{"{stand.repo}": ("{stand.fassung}", "{summe}")}}')
    typer.echo("Den Ordner so hochladen, dass <MODEL_MIRROR_URL>/<repo>/<fassung>/manifest.json erreichbar ist.")


@app.command("recap-probe")
def recap_probe(
    session: str = typer.Argument(None, help="Session-ID (ohne Angabe: Liste der Sessions mit Transkript)"),
    lokal: bool = typer.Option(False, "--lokal", help="Ollama auf diesem Computer statt der Cloud-API"),
    modell: str = typer.Option(None, "--modell", help="Anderes Modell als in der Verwaltung eingestellt"),
):
    """Recap und Vorschläge für eine vorhandene Session erzeugen und nur anzeigen – nichts wird gespeichert.
    Zum Vergleichen von Modellen (Cloud-API gegen lokales Modell)."""
    import time

    from sqlalchemy import func, select

    from app.config import get_settings
    from app.einstellungen import llm_konfig
    from app.models import Campaign, GameSession, TranscriptSegment
    from app.sprachmodell import Ablauf, OllamaKlient, SprachmodellFehler
    from app.zusammenfassung import api_klient, eingabe_bauen, recap_eingabe, vorschlag_eingabe

    with _db() as db:
        if not session:
            zeilen = db.execute(select(GameSession, Campaign.title, func.count(TranscriptSegment.id))
                                .join(Campaign, Campaign.id == GameSession.campaign_id)
                                .join(TranscriptSegment, TranscriptSegment.session_id == GameSession.id)
                                .group_by(GameSession.id).order_by(GameSession.created_at.desc()).limit(20)).all()
            if not zeilen:
                typer.echo("Noch keine Session mit Transkript.")
            for s, titel, n in zeilen:
                typer.echo(f"{s.id}  {titel} · Kapitel {s.number} · {n} Zeilen · {s.state}")
            return
        s = db.get(GameSession, session)
        if s is None:
            typer.echo("Session nicht gefunden. Ohne Angabe aufrufen, um die Liste zu sehen.", err=True)
            raise typer.Exit(1)
        basis = eingabe_bauen(db, s)
        if not basis.transkript:
            typer.echo("Diese Session hat (noch) kein Transkript.", err=True)
            raise typer.Exit(1)
        recap_ein, vorschlag_ein = recap_eingabe(basis), vorschlag_eingabe(db, s, basis)
        k = llm_konfig(db)
    if lokal:
        klient = OllamaKlient(get_settings().worker_llm_url, modell or k.lokal_modell, k.lokal_kontext)
        if klient.version() is None:
            typer.echo(f"Ollama ist unter {klient.url} nicht erreichbar (docs/ENTWICKLUNG.md, „Einen Worker von Hand betreiben“).", err=True)
            raise typer.Exit(1)
        klient.bereitstellen(typer.echo)
        ablauf = Ablauf(klient, max_transkript_tokens=max(2000, klient.kontext - 5000),
                        stueck_tokens=max(1500, (klient.kontext - 4000) // 2))
    else:
        if not k.api_key:
            typer.echo("Kein API-Schlüssel hinterlegt (Verwaltung → Zusammenfassung).", err=True)
            raise typer.Exit(1)
        if modell:
            k.api_modell = modell
        klient = api_klient(k)
        ablauf = Ablauf(klient)
    typer.echo(f"Modell {klient.modell}, {len(recap_ein['transkript'])} Transkriptzeilen …")
    t0 = time.monotonic()
    try:
        d = ablauf.ausfuehren(recap_ein, vorschlag_ein, lambda p: None)
    except SprachmodellFehler as e:
        typer.echo(f"Fehlgeschlagen: {e}", err=True)
        raise typer.Exit(1)
    finally:
        if lokal:
            klient.entladen()
    dauer = time.monotonic() - t0
    typer.echo(f"\n# {d['title']}\n\n{d['text']}\n")
    if d["openThreads"]:
        typer.echo("Offene Fäden:\n" + "\n".join(f"- {f}" for f in d["openThreads"]))
    typer.echo(f"\nVorschläge ({len(d['proposals'])}):")
    for v in d["proposals"]:
        typer.echo(f"- {v['action']:6} {v['entryType']:8} {v['title']} ({v['suggestedVisibility']}, "
                   f"{v['confidence']:.1f}{', ' + ', '.join(v['flags']) if v['flags'] else ''})\n    {v['detail'][:300]}")
    kosten = klient.kosten_cent(d["tokensIn"], d["tokensOut"])
    typer.echo(f"\n{ablauf.zaehler.aufrufe} Aufrufe · {d['tokensIn']} Tokens ein, {d['tokensOut']} aus · "
               f"etwa {kosten} Cent · {dauer:.0f} s")


@app.command("modellvergleich")
def modellvergleich(
    server: str = typer.Option(..., "--server", help="Adresse des Taleward-Servers, z. B. https://taleward.example.org"),
    benutzer: str = typer.Option(..., "--benutzer", help="Benutzername einer SL der Kampagne"),
    passwort: str = typer.Option(..., "--passwort", prompt=True, hide_input=True),
    session: str = typer.Option(None, "--session", help="Session-ID (ohne Angabe: Liste der Sessions mit Transkript)"),
    modelle: str = typer.Option("ministral-3:8b,qwen3:8b,qwen3.5:9b,gemma4:e4b,gemma4:12b", "--modelle", help="Kommagetrennt; Kontext je Modell mit @, z. B. gemma4:12b@8192"),
    kontext: int = typer.Option(12288, "--kontext", help="Kontextgröße (Tokens), wie im Worker"),
    richter: str = typer.Option("qwen3:8b", "--richter", help="Modell, das alle Recaps bewertet; leer = ohne"),
    ollama: str = typer.Option(None, "--ollama", help="Adresse von Ollama (Standard: 11434, sonst 11435 der Worker-App)"),
    ziel: Path = typer.Option(Path("."), "--ziel", help="Ordner für die Ergebnisse"),
):
    """Mehrere lokale Sprachmodelle schreiben Recap, Gegenprüfung und Vorschläge für dieselbe Session – zum
    Vergleichen am eigenen PC. Liest nur über die Schnittstelle, ändert nichts auf dem Server."""
    from app import modellvergleich as mv

    try:
        s = mv.Server(server)
        s.anmelden(benutzer, passwort)
        if not session:
            for x in s.sessions_mit_transkript():
                typer.echo(f"{x['id']}  {x['kampagne']} · Kapitel {x['number']} · {x.get('title') or ''} · {x['state']}")
            typer.echo("\nNoch einmal mit --session <ID> aufrufen.")
            return
        url = mv.ollama_finden(ollama)
        typer.echo(f"Ollama unter {url}")
        ordner = mv.ausfuehren(s, session, mv.modelle_lesen(modelle, kontext), richter.strip(), kontext, url, ziel)
    except mv.VergleichFehler as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1)
    typer.echo(f"\nFertig. Bericht: {(ordner / 'bericht.html').resolve()}")


@app.command("unterlage-probe")
def unterlage_probe(
    datei: Path = typer.Argument(..., help="PDF, .docx, .txt oder .md"),
    art: str = typer.Option("gm", "--art", help="handout | gm | mixed"),
    kampagne: str = typer.Option(None, "--kampagne", help="Kampagnen-ID: deren Bibel als Kontext verwenden"),
    lokal: bool = typer.Option(False, "--lokal", help="Ollama auf diesem Computer statt der Cloud-API"),
    modell: str = typer.Option(None, "--modell", help="Anderes Modell als in der Verwaltung eingestellt"),
):
    """Eine Unterlage auswerten und die Vorschläge nur anzeigen – nichts wird gespeichert."""
    import time

    from sqlalchemy import select

    from app.config import get_settings
    from app.einstellungen import llm_konfig
    from app.models import Campaign, Entry
    from app.sprachmodell import DokumentAblauf, OllamaKlient, SprachmodellFehler
    from app.unterlagen import art_der_datei, auslesen
    from app.zusammenfassung import api_klient

    if art not in ("handout", "gm", "mixed"):
        typer.echo("--art muss handout, gm oder mixed sein.", err=True)
        raise typer.Exit(1)
    daten = datei.expanduser().read_bytes()
    x = auslesen(art_der_datei(datei.name, daten), daten)
    typer.echo(f"{len(x.abschnitte)} Abschnitte, {x.seiten or '?'} Seiten, davon {x.leere_seiten} ohne Text")
    if not x.abschnitte:
        raise typer.Exit(1)
    with _db() as db:
        c = db.get(Campaign, kampagne) if kampagne else None
        bibel = [{"id": e.id, "typ": e.type, "name": e.name, "zusammenfassung": e.summary or "",
                  "gm_notes": e.gm_notes, "sichtbarkeit": e.visibility}
                 for e in db.scalars(select(Entry).where(Entry.campaign_id == c.id))] if c else []
        k = llm_konfig(db)
    ein = {"sprache": c.language if c else "de", "kampagne": c.title if c else "Probe", "system": None,
           "system_name": None, "welt": c.world_info if c else None, "art": art, "titel": datei.stem,
           "abschnitte": x.abschnitte, "bibel": bibel}
    if lokal:
        klient = OllamaKlient(get_settings().worker_llm_url, modell or k.lokal_modell, k.lokal_kontext)
        if klient.version() is None:
            typer.echo(f"Ollama ist unter {klient.url} nicht erreichbar.", err=True)
            raise typer.Exit(1)
        klient.bereitstellen(typer.echo)
        ablauf = DokumentAblauf(klient, stueck_tokens=max(1500, (klient.kontext - 5000) // 2))
    else:
        if not k.api_key:
            typer.echo("Kein API-Schlüssel hinterlegt (Verwaltung → Zusammenfassung).", err=True)
            raise typer.Exit(1)
        if modell:
            k.api_modell = modell
        klient = api_klient(k)
        ablauf = DokumentAblauf(klient)
    t0 = time.monotonic()
    try:
        d = ablauf.ausfuehren(ein)
    except SprachmodellFehler as e:
        typer.echo(f"Fehlgeschlagen: {e}", err=True)
        raise typer.Exit(1)
    finally:
        if lokal:
            klient.entladen()
    typer.echo(f"\nVorschläge ({len(d['proposals'])}) – vor den Regeln für „{art}“, die der Server beim Speichern "
               "anwendet:")
    for v in d["proposals"]:
        seiten = ", ".join(str(b["page"]) for b in v["evidence"] if b.get("page"))
        typer.echo(f"- {v['action']:6} {v['entryType']:8} {v['title']}"
                   f"{' (Seite ' + seiten + ')' if seiten else ''}{' [öffentlich?]' if v['publicSuggested'] else ''}"
                   f"\n    Spieler: {v['detail'][:300] or '–'}\n    SL:      {(v['gmNotes'] or '–')[:300]}")
    if d["worldInfoSuggestion"]:
        typer.echo(f"\nWelt-Hintergrund:\n{d['worldInfoSuggestion']}")
    typer.echo(f"\n{ablauf.zaehler.aufrufe} Aufrufe · {d['tokensIn']} Tokens ein, {d['tokensOut']} aus · "
               f"etwa {klient.kosten_cent(d['tokensIn'], d['tokensOut'])} Cent · {time.monotonic() - t0:.0f} s")


@app.command("sicherung")
def sicherung_anlegen():
    """Jetzt eine vollständige Sicherung anlegen (Datenbank, Bilder, SL-Unterlagen)."""
    from app import sicherung

    with _db() as db:
        e = sicherung.einstellungen(db)
    p = sicherung.erstellen("manuell", e["ordner"], e["tage"])
    typer.echo(f"Sicherung angelegt: {p}")


@app.command("wiederherstellen")
def wiederherstellen(datei: Path = typer.Argument(..., help="Sicherung (ZIP) aus der Verwaltung oder data/sicherungen"),
                     ja: bool = typer.Option(False, "--ja", help="Ohne Rückfrage")):
    """Eine Sicherung zurückspielen. Vorher den Server beenden! Der jetzige Stand wird vorher selbst gesichert."""
    from app import sicherung

    try:
        info = sicherung.pruefen(datei.expanduser())
    except sicherung.SicherungFehler as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1)
    typer.echo(f"Sicherung vom {info.get('erstellt', '?')} mit {info.get('dateien', 0)} Dateien.")
    if not ja and not typer.confirm("Den jetzigen Stand ersetzen? (Er wird vorher gesichert.)"):
        raise typer.Exit(1)
    vorher = sicherung.wiederherstellen(datei.expanduser())
    if vorher:
        typer.echo(f"Bisheriger Stand gesichert: {vorher}")
    run_migrations()  # ältere Sicherung auf den Stand dieser Version bringen
    typer.echo("Wiederhergestellt. Jetzt den Server starten.")


@app.command("einrichtungscode")
def einrichtungscode(neu: bool = typer.Option(False, "--neu", help="Einen neuen Code erzeugen (der alte gilt nicht mehr)")):
    """Link für die Ersteinrichtung anzeigen (nur, solange es noch keinen Verwalter gibt)."""
    from app.einrichtung import braucht_einrichtung, code_erzeugen

    with _db() as db:
        if not braucht_einrichtung(db):
            typer.echo("Dieser Server ist schon eingerichtet. Verwalter-Recht vergeben mit: uv run chronik admin -u NAME")
            raise typer.Exit(1)
        code = code_erzeugen(db, neu=neu)
        from app.einstellungen import angaben

        adresse = angaben(db).public_url
    if adresse:  # z. B. im Docker-Paket: PUBLIC_URL=https://taleward.meinverein.de
        typer.echo(f"Im Browser öffnen:  {adresse.rstrip('/')}/verwaltung/einrichtung?code={code}")
        return
    typer.echo(f"Im Browser öffnen:  <Adresse dieses Servers>/verwaltung/einrichtung?code={code}")
    typer.echo(f"Beispiel am eigenen PC: http://localhost:8000/verwaltung/einrichtung?code={code}")


@app.command("stimmen-vergleich")
def stimmen_vergleich(dateien: list[Path] = typer.Argument(..., help="Zwei oder mehr Aufnahmen mit je einer Person")):
    """Stimmabdrücke mehrerer Aufnahmen vergleichen – zum Einstellen der Wiedererkennung (braucht Grafikkarte).

    Beispiel: zwei Aufnahmen von dir und eine von jemand anderem. Gleiche Person sollte deutlich höher liegen."""
    import shutil
    import tempfile

    from app import audio
    from app.config import get_settings
    from app.stimmprofile import kosinus, sicherheit
    from app.transkription import EinrichtungsFehler, WhisperXMotor

    if len(dateien) < 2:
        typer.echo("Bitte mindestens zwei Dateien angeben.", err=True)
        raise typer.Exit(1)
    s = get_settings()
    motor = WhisperXMotor(s.whisper_model, s.whisper_compute_type, s.whisper_batch, s.hf_token)
    try:
        motor.pruefen()
    except EinrichtungsFehler as e:
        typer.echo(f"Geht nicht: {e}", err=True)
        raise typer.Exit(1)
    arbeit = Path(tempfile.mkdtemp(prefix="stimmen-"))
    abdruecke = []
    try:
        for d in dateien:
            wav = arbeit / "a.wav"
            audio.zusammenfuegen([d], wav)
            v, sprechzeit = motor.stimmabdruck(motor.audio_laden(wav), lambda _p: None)
            abdruecke.append(v)
            typer.echo(f"{d.name}: {sprechzeit:.0f} s Sprache erkannt")
    finally:
        shutil.rmtree(arbeit, ignore_errors=True)  # keine Aufnahme bleibt liegen
    typer.echo("\nÄhnlichkeit (1 = gleich) → Sicherheit des Vorschlags:")
    for i in range(len(dateien)):
        for j in range(i + 1, len(dateien)):
            sim = kosinus(abdruecke[i], abdruecke[j])
            typer.echo(f"  {dateien[i].name} ↔ {dateien[j].name}: {sim:.2f} → {sicherheit(sim):.0%}")


@app.command("extern-probe")
def extern_probe(
    datei: Path = typer.Argument(..., help="Audiodatei, z. B. die Probefolge"),
    dauer: float = typer.Option(10, "--dauer", help="Nur so viele Minuten schicken (Kosten gering halten)"),
    namen: str = typer.Option("", "--namen", help="Namenshilfe, kommagetrennt"),
):
    """Externe Transkription (Mistral Voxtral) mit einer Datei ausprobieren – zum Vergleich mit dem Probelauf.

    Braucht MISTRAL_API_KEY in der .env. Die Datei geht an Mistral; nur mit Aufnahmen, deren Weitergabe erlaubt ist."""
    import shutil
    import subprocess
    import tempfile

    from app.audio import AudioFehler
    from app.extern import MistralKlient, _segmente, _teile

    from app.einstellungen import extern_konfig

    k = extern_konfig(_db())
    if not k.api_key:
        typer.echo("Kein Mistral-Schlüssel – in der Verwaltung unter „Transkription“ oder als MISTRAL_API_KEY in der .env.",
                   err=True)
        raise typer.Exit(1)
    arbeit = Path(tempfile.mkdtemp(prefix="extern-probe-"))
    try:
        ausschnitt = arbeit / "ausschnitt.wav"
        subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-t", str(dauer * 60), "-i", str(datei), "-ac", "1",
                        "-ar", "16000", str(ausschnitt)], check=True)
        klient = MistralKlient(k)
        segmente, sekunden = [], 0.0
        for teil, versatz in _teile(ausschnitt, arbeit, k.max_sekunden):
            antwort = klient.transkribieren(teil, True, [n.strip() for n in namen.split(",") if n.strip()])
            sekunden += float((antwort.get("usage") or {}).get("prompt_audio_seconds") or 0)
            segmente += _segmente(antwort, versatz, lambda r: r)
    except AudioFehler as e:
        typer.echo(f"Fehlgeschlagen: {e}", err=True)
        raise typer.Exit(1)
    finally:
        shutil.rmtree(arbeit, ignore_errors=True)
    redezeit: dict = {}
    for seg in segmente:
        redezeit[seg["speaker"]] = redezeit.get(seg["speaker"], 0) + seg["end"] - seg["start"]
    typer.echo(f"{sekunden / 60:.1f} min abgerechnet ≈ "
               f"{sekunden / 60 * k.cent_pro_minute:.1f} Cent")
    typer.echo(f"{len(redezeit)} Stimme(n): " + ", ".join(f"{k}: {v / 60:.1f} min" for k, v in
                                                        sorted(redezeit.items(), key=lambda x: -x[1])))
    for seg in segmente[:12]:
        typer.echo(f"  [{int(seg['start']) // 60:02d}:{int(seg['start']) % 60:02d}] {seg['speaker']}: {seg['text'].strip()[:90]}")


@app.command("serve")
def serve(
    host: str = typer.Option("0.0.0.0", help="0.0.0.0 = im Heimnetz erreichbar"),
    port: int = typer.Option(8000),
    reload: bool = typer.Option(False, help="Bei Codeänderungen neu starten (Entwicklung)"),
):
    """Server starten."""
    import os

    import uvicorn

    from app.config import get_settings

    # Der eigene Worker (Verwaltung) verbindet sich über diesen Port
    if "WORKER_SERVER_URL" not in os.environ and port != 8000:
        os.environ["WORKER_SERVER_URL"] = f"http://127.0.0.1:{port}"
        get_settings.cache_clear()

    uvicorn.run(
        "app.main:app", host=host, port=port, reload=reload,
        proxy_headers=True, forwarded_allow_ips=get_settings().forwarded_allow_ips,
    )


if __name__ == "__main__":
    app()
