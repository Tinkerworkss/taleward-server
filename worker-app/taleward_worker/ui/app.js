/* Taleward Worker – Oberfläche. Fragt den Zustand bei der App ab (pywebview js_api) und zeigt ihn an. */
(function () {
  "use strict";
  const $ = (s) => document.querySelector(s);
  const $$ = (s) => Array.from(document.querySelectorAll(s));
  let api = null, S = null, sprache = "de", reiter = "status", schritt = null, fertigZeigen = false, pruefErgebnis = null;
  let letzterLog = "", beschaeftigt = false, testWahl = null, prozessorWahl = false;
  const testmodus = () => (testWahl !== null ? testWahl : !!(S && S.einstellungen.testmodus));

  // ------------------------------------------------------------ Texte
  function t(k, v) {
    const tab = window.TEXTE[sprache] || {};
    let s = tab[k] !== undefined ? tab[k] : (window.TEXTE.de[k] !== undefined ? window.TEXTE.de[k] : k);
    if (v) for (const [a, b] of Object.entries(v)) s = s.split("{" + a + "}").join(b);
    return s;
  }
  function uebersetzen() {
    document.documentElement.lang = sprache;
    $$("[data-t]").forEach((el) => { el.textContent = t(el.dataset.t); });
  }
  function spracheWaehlen() {
    const eigen = S && S.einstellungen.sprache;
    const neu = eigen || ((navigator.language || "de").toLowerCase().startsWith("de") ? "de" : "en");
    if (neu !== sprache) { sprache = neu; uebersetzen(); }
  }
  const zahl = (n, st = 0) => Number(n).toLocaleString(sprache === "de" ? "de-DE" : "en-GB", { maximumFractionDigits: st, minimumFractionDigits: st });
  const gb = (mb) => mb >= 1024 ? zahl(mb / 1024, 1) + " GB" : zahl(mb) + " MB";
  function wann(zeit) {
    if (!zeit) return t("gleich");
    const s = Math.round(zeit - Date.now() / 1000);
    return s > 1 ? t("neuer_versuch_in", { s }) : t("gleich");
  }
  function toast(text) {
    const el = $("#toast"); el.textContent = text; el.hidden = false;
    clearTimeout(toast.z); toast.z = setTimeout(() => { el.hidden = true; }, 3200);
  }

  // ------------------------------------------------------------ Ansicht wählen
  function imAssistenten() {
    if (!S) return false;
    if (!S.einstellungen.gekoppelt) return true;
    if (fertigZeigen) return true;
    if (!S.motor && !S.entwicklung) return true;
    return false;
  }
  function assistentSchritt() {
    if (!S.einstellungen.gekoppelt) return 1;
    const inst = S.installation;
    const offen = inst && inst.phase !== "fertig";
    if (offen && (schritt === 3 || !S.motor)) return 3;
    if (fertigZeigen && S.motor) return 4;
    if (schritt === 3) return 3;
    return 2;
  }
  function zeigeReiter(name) {
    reiter = name;
    $$(".reiter button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.reiter === name)));
    ["status", "einstellungen", "protokoll"].forEach((r) => { $("#reiter-" + r).hidden = r !== name || imAssistenten(); });
    if (name === "einstellungen") speicherLaden();
    if (name === "protokoll") logLaden(true);
  }

  // ------------------------------------------------------------ Darstellung
  function darstellen() {
    spracheWaehlen();
    const assistent = imAssistenten();
    $("#kopf").classList.toggle("kopf--assistent", assistent && reiter !== "protokoll");
    $("#assistent").hidden = !assistent || reiter === "protokoll";
    ["status", "einstellungen", "protokoll"].forEach((r) => {
      $("#reiter-" + r).hidden = r !== reiter || (assistent && r !== "protokoll");
    });
    if (assistent) assistentZeigen(); else statusZeigen();
    einstellungenZeigen();
  }

  function assistentZeigen() {
    const nr = assistentSchritt();
    $$(".schritte li").forEach((li) => {
      const n = Number(li.dataset.schritt);
      li.dataset.stand = n < nr ? "fertig" : n === nr ? "aktiv" : "";
    });
    $$("[data-seite]").forEach((el) => { el.hidden = Number(el.dataset.seite) !== nr; });
    if (nr === 1) {
      const name = $("#name");
      if (!name.value && S.einstellungen.name) name.value = S.einstellungen.name;
      const adr = $("#adresse");
      if (!adr.value && S.einstellungen.server) adr.value = S.einstellungen.server;
    }
    if (nr === 2) pruefungZeigen();
    if (nr === 3) installationZeigen();
    if (nr === 4) { $("#hinweis-modelle").hidden = !!(S.motor && S.motor.testmodus); reglerZeigen(); }
  }

  function dauerText(d) {
    if (!d) return "";
    return d[1] >= 120 ? t("dauer_std", { a: zahl(d[0] / 60, d[0] % 60 ? 1 : 0), b: zahl(d[1] / 60, d[1] % 60 ? 1 : 0) })
      : t("dauer_min", { a: d[0], b: d[1] });
  }
  function profilText(p) {
    if (!p) return "–";
    if (p.geraet === "cpu") return t("pw_cpu", { modell: p.modell });
    let s = t(p.ausrichten === "cpu" ? "pw_gpu_teil" : "pw_gpu", { modell: p.modell });
    if (p.grenzeMb) s += " · " + t("pw_grenze", { gb: gb(p.grenzeMb) });
    return s;
  }

  function pruefungZeigen() {
    $("#verbunden-mit").textContent = t("verbunden_mit", { server: S.serverName || S.einstellungen.server, name: S.einstellungen.name });
    const h = S.hardware;
    if (!h) return;
    const k = h.grafikkarte, gpu = h.grafikkarteNutzbar;
    const zeilen = [];
    if (gpu) zeilen.push(["ja", t("pr_gpu"), `${k.name} · ${gb(k.vram_mb)}`]);
    else if (k && k.vram_mb < 2000) zeilen.push(["warn", t("pr_gpu"), t("pr_gpu_klein", { name: k.name, vram: gb(k.vram_mb) })]);
    else if (k && !h.treiberOk) zeilen.push(["warn", t("pr_treiber"), t("pr_treiber_alt", { v: k.treiber, min: h.minTreiber })]);
    else zeilen.push(["warn", t("pr_gpu"), t("pr_gpu_keine_cpu")]);
    if (gpu) zeilen.push(["ja", t("pr_treiber"), k.treiber]);
    if (h.ramGb) zeilen.push([h.ramOk ? "ja" : "warn", t("pr_ram"), h.ramOk ? t("pr_ram_text", { gb: zahl(h.ramGb) }) : t("pr_ram_wenig", { gb: zahl(h.ramGb), min: h.minRamGb })]);
    zeilen.push([h.platzOk ? "ja" : "warn", t("pr_platz"), t("pr_platz_text", { frei: zahl(h.platzGb), noetig: h.platzNoetigGb })]);
    const p = h.empfehlung;
    zeilen.push([p.geraet === "cpu" ? "warn" : "ja", t("pr_arbeitsweise"), profilText(p) + ". " + t("pw_dauer", { dauer: dauerText(p.dauerMin) })]);
    const liste = $("#pruefliste");
    const stand = JSON.stringify(zeilen);
    if (liste.dataset.stand !== stand) {
      liste.innerHTML = zeilen.map(([ok]) => `<li data-ok="${ok}"><span class="zeichen">${ok === "ja" ? "✓" : "!"}</span><span class="titel"></span><span class="wert"></span></li>`).join("");
      Array.from(liste.children).forEach((li, i) => {
        li.querySelector(".titel").textContent = zeilen[i][1];
        li.querySelector(".wert").textContent = zeilen[i][2];
      });
      liste.dataset.stand = stand;
    }
    $("#pruef-meldung").hidden = gpu;
    $("#pruef-meldung").className = "meldung meldung--hinweis";
    $("#pruef-meldung").textContent = t("pr_cpu_angebot");
    $("#testmodus-waehlen").hidden = gpu;
    $("#prozessor-waehlen").hidden = gpu;
    $("#weiter-installieren").hidden = !gpu;
  }

  // ------------------------------------------------------------ Schieberegler Grafikspeicher
  function reglerZeigen() {
    const k = S.hardware && S.hardware.grafikkarte;
    const sichtbar = !!(k && S.hardware.grafikkarteNutzbar && S.einstellungen.geraet !== "cpu" && !S.einstellungen.testmodus);
    const maxGb = k ? Math.floor(k.vram_mb / 512) / 2 : 8;
    $$("[data-regler]").forEach((r) => {
      r.hidden = !sichtbar;
      if (!sichtbar) return;
      const ein = r.querySelector("[data-regler-eingabe]");
      if (ein.max != maxGb) ein.max = maxGb;
      r.querySelector("[data-regler-max]").textContent = zahl(maxGb, maxGb % 1 ? 1 : 0) + " GB";
      if (document.activeElement !== ein && !ein.dataset.zieht) {
        const g = S.einstellungen.vram_grenze_mb;
        ein.value = g ? Math.min(maxGb, g / 1024) : maxGb;
        reglerText(r, S.profil);
      }
    });
  }
  function reglerWertMb(ein) {
    const v = Number(ein.value);
    return v >= Number(ein.max) ? 0 : Math.round(v * 1024);
  }
  function reglerText(r, p) {
    const ein = r.querySelector("[data-regler-eingabe]");
    const v = Number(ein.value), max = Number(ein.max);
    r.querySelector("[data-regler-wert]").textContent = v >= max ? t("regler_alles") + " · " + zahl(max, max % 1 ? 1 : 0) + " GB"
      : t("regler_von", { a: zahl(v, v % 1 ? 1 : 0) + " GB", b: zahl(max, max % 1 ? 1 : 0) + " GB" });
    if (!p) return;
    const folge = r.querySelector("[data-regler-folge]");
    folge.textContent = "";
    const st = document.createElement("strong"); st.textContent = profilText(p);
    folge.append(st, document.createElement("br"), document.createTextNode(t("pw_dauer", { dauer: dauerText(p.dauerMin) })));
    if (S.dienst.neustartOffen) folge.append(document.createElement("br"), document.createTextNode(t("neustart_offen")));
  }

  function installationZeigen() {
    const inst = S.installation;
    const test = testmodus();
    $("#installieren-text").textContent = test ? t("installieren_text_test")
      : prozessorWahl ? t("installieren_text_cpu", { mb: gb(1300) })
      : t("installieren_text_windows", { mb: gb(S.system === "windows" ? 3600 : 5200) });
    const laeuft = inst && !["fertig", "fehler", "abgebrochen"].includes(inst.phase);
    const fehler = inst && ["fehler", "abgebrochen"].includes(inst.phase);
    $("#inst-start").hidden = laeuft || fehler;
    $("#inst-lauf").hidden = !laeuft;
    $("#inst-fehler").hidden = !fehler;
    if (laeuft) {
      $("#inst-phase").textContent = t("ph_" + inst.phase);
      const p = Math.round(inst.anteil * 100);
      $("#inst-balken span").style.width = p + "%";
      $("#inst-prozent").textContent = p + " %";
      $("#inst-mb").textContent = inst.phase === "pakete" && inst.erwartetMb ? t("mb_von", { a: gb(inst.geladenMb), b: gb(inst.erwartetMb) }) : "";
      $("#inst-zeile").textContent = inst.letzteZeile || "";
    }
    if (fehler) $("#inst-fehlertext").textContent = t("f_" + inst.fehler);
  }

  function statusZeigen() {
    const d = S.dienst, z = d.zustand, art = z.art;
    const karte = $("#statuskarte");
    karte.dataset.art = art;
    let titel = t("st_" + art), text = t("st_" + art + "_text", { server: S.serverName || S.einstellungen.server, wann: wann(z.neuerVersuch) });
    let fortschritt = null;
    if (art === "arbeitet") {
      text = t("st_" + (z.typ || "transcribe"));
      fortschritt = z.p || 0;
    } else if (art === "startet" && z.modelle) {
      text = t("st_modelle_text", { mb: gb(z.modelleMb || 0) });
    } else if (art === "fehler") {
      text = z.text || t("f_" + (z.code || "einrichtung"));
    }
    if (d.info && d.info.testmodus && art === "warte") titel += " · " + t("testmodus_aktiv");
    $("#status-titel").textContent = titel;
    $("#status-text").textContent = text;
    $("#status-fortschritt").hidden = fortschritt === null;
    if (fortschritt !== null) {
      $("#status-fortschritt .balken span").style.width = Math.round(fortschritt * 100) + "%";
      $("#status-prozent").textContent = Math.round(fortschritt * 100) + " %";
      $("#status-dauer").textContent = t("laufzeit", { min: Math.max(1, Math.round((Date.now() / 1000 - z.seit) / 60)) });
    }
    knoepfe(art);
    $("#z-heute").textContent = zahl(d.statistik.heute);
    $("#z-gesamt").textContent = zahl(d.statistik.gesamt);
    $("#i-server").textContent = (S.serverName ? S.serverName + " · " : "") + S.einstellungen.server;
    $("#i-name").textContent = S.einstellungen.name;
    const k = S.hardware && S.hardware.grafikkarte;
    $("#i-gpu").textContent = d.info.gpu || (k ? `${k.name} · ${gb(k.vram_mb)}` : "–");
    $("#i-modell").textContent = (d.info.testmodus || S.einstellungen.testmodus) ? t("testmodus_aktiv") : profilText(S.profil);
    const m = d.statistik.messung;
    let mt = t("noch_keine_messung");
    if (m && m.audio) {
      const dauer = (s) => s >= 3600 ? zahl(s / 3600, 1) + " h" : Math.max(1, Math.round(s / 60)) + " min";
      mt = t("messung_text", { audio: dauer(m.audio), rechen: dauer(m.rechen) });
      if (m.peakMb) mt += " · " + t("messung_peak", { gb: gb(m.peakMb) });
    }
    $("#i-messung").textContent = mt;
    $("#i-fassung").textContent = t("fassung_text", { app: S.version, motor: S.motor ? S.motor.fassung || S.motor.ref : "–" });
    // Hinweise
    const inst = S.installation;
    const upd = $("#motor-update");
    const laeuft = inst && S.motor && !["fertig", "fehler", "abgebrochen"].includes(inst.phase);
    upd.hidden = !laeuft;
    if (laeuft) upd.textContent = t("st_update") + " – " + t("st_update_text", { fassung: inst.fassung, prozent: Math.round(inst.anteil * 100) + " %" });
    const au = $("#app-update"), u = S.appUpdate;
    au.hidden = !u;
    if (u) {
      const schluessel = [u.version, u.phase, Math.round(u.anteil * 20), u.fehler, S.einstellungen.auto_update, sprache].join("|");
      if (au.dataset.s !== schluessel) {
        au.dataset.s = schluessel;
        au.className = "meldung " + (u.phase === "fehler" ? "meldung--fehler" : "meldung--hinweis");
        au.innerHTML = "";
        const st = document.createElement("strong");
        st.textContent = t("app_update", { version: u.version });
        const text = document.createElement("span");
        text.textContent = u.phase === "laden" ? t("app_update_laden", { prozent: Math.round(u.anteil * 100) + " %" })
          : u.phase === "installieren" || u.phase === "fertig" ? t("app_update_installieren")
          : u.phase === "fehler" ? t("app_update_fehler", { fehler: u.fehler })
          : !u.moeglich ? t("app_update_hand")
          : S.einstellungen.auto_update ? t("app_update_auto") : "";
        au.append(st, text);
        if (u.moeglich && (u.phase === "bereit" || u.phase === "fehler")) {
          const b = document.createElement("button"); b.className = "tw-btn tw-btn--secondary";
          b.textContent = t("Jetzt aktualisieren");
          b.onclick = () => { api.app_aktualisieren(); setTimeout(neuLaden, 300); };
          au.append(b);
        }
        if (u.notizen) {
          const d = document.createElement("details"), s = document.createElement("summary"), p = document.createElement("p");
          s.textContent = t("Was ist neu?"); p.textContent = u.notizen; p.className = "notizen";
          d.append(s, p); au.append(d);
        }
      }
    }
  }

  function knoepfe(art) {
    const ziel = $("#status-knoepfe");
    const liste = [];
    if (["warte", "arbeitet", "getrennt", "startet"].includes(art)) liste.push(["Pausieren", "secondary", () => api.pausieren(true)]);
    if (art === "pausiert") liste.push(["Fortsetzen", "primary", () => api.pausieren(false)]);
    if (art === "gestoppt") liste.push(["Starten", "primary", () => api.starten()]);
    if (art === "fehler" || art === "abgestuerzt") {
      if ((S.dienst.zustand.code || "") === "motor_fehlt") liste.push(["Installieren", "primary", () => { schritt = 2; }]);
      else liste.push(["Erneut versuchen", "primary", () => api.starten()]);
      liste.push(["Protokoll ansehen", "quiet", () => zeigeReiter("protokoll")]);
    }
    if (art === "abgelehnt") liste.push(["Neu koppeln", "primary", () => api.entkoppeln().then(neuLaden)]);
    const schluessel = art + "|" + liste.map((x) => x[0]).join(",") + "|" + sprache;
    if (ziel.dataset.s === schluessel) return;
    ziel.dataset.s = schluessel;
    ziel.innerHTML = "";
    liste.forEach(([text, stil, fn]) => {
      const b = document.createElement("button");
      b.className = "tw-btn tw-btn--" + stil; b.textContent = t(text);
      b.onclick = () => { fn(); setTimeout(neuLaden, 150); };
      ziel.append(b);
    });
    ziel.hidden = liste.length === 0;
  }

  function einstellungenZeigen() {
    $$("[data-einstellung]").forEach((el) => {
      if (el === document.activeElement) return;
      const n = el.dataset.einstellung;
      const wert = n === "autostart" ? S.autostart : S.einstellungen[n];
      if (el.type === "checkbox") el.checked = !!wert; else el.value = wert || (n === "modell" ? "auto" : "");
    });
    $("#fuss-version").textContent = "Taleward Worker " + S.version;
    const k = S.hardware && S.hardware.grafikkarte;
    $("#zeile-prozessor").hidden = !(k && S.hardware.grafikkarteNutzbar) || S.einstellungen.testmodus;
    if (document.activeElement !== $("#nur-prozessor")) $("#nur-prozessor").checked = S.einstellungen.geraet === "cpu";
    reglerZeigen();
  }

  async function speicherLaden() {
    const s = await api.speicherbelegung();
    $("#speicher").textContent = t("speicher_text", { motor: gb(s.motorMb), modelle: gb(s.modelleMb) });
  }

  async function logLaden(ganz) {
    if (reiter !== "protokoll") return;
    const zeilen = await api.protokoll();
    const text = zeilen.join("\n");
    if (text === letzterLog && !ganz) return;
    letzterLog = text;
    const el = $("#log");
    const unten = el.scrollTop + el.clientHeight >= el.scrollHeight - 30;
    el.textContent = text;
    if (unten || ganz) el.scrollTop = el.scrollHeight;
  }

  async function neuLaden() {
    try { S = await api.zustand(); } catch (e) { return; }
    darstellen();
  }

  // ------------------------------------------------------------ Aktionen
  function binden() {
    $$(".reiter button").forEach((b) => { b.onclick = () => zeigeReiter(b.dataset.reiter); });
    $$("[data-zeige]").forEach((b) => { b.onclick = () => zeigeReiter(b.dataset.zeige); });
    $$("[data-zurueck]").forEach((b) => { b.onclick = () => zeigeReiter("status"); });

    const adresse = $("#adresse");
    adresse.addEventListener("input", () => {
      const http = /^http:\/\//i.test(adresse.value.trim()) && !/^http:\/\/(localhost|127\.0\.0\.1)/i.test(adresse.value.trim());
      $("#unsicher-zeile").hidden = !http;
    });
    $("#code").addEventListener("input", (e) => {
      let v = e.target.value.toUpperCase().replace(/[^A-Z0-9]/g, "").slice(0, 8);
      if (v.length > 4) v = v.slice(0, 4) + "-" + v.slice(4);
      e.target.value = v;
    });
    $("#verbinden").onclick = async () => {
      if (beschaeftigt) return;
      const fehler = $("#koppeln-fehler");
      fehler.hidden = true;
      if ($("#code").value.replace("-", "").length < 8) { fehler.textContent = t("f_code_fehlt"); fehler.hidden = false; return; }
      beschaeftigt = true; $("#verbinden").disabled = true;
      try {
        const pr = await api.server_pruefen(adresse.value);
        if (!pr.ok) { fehler.textContent = t("f_" + pr.fehler); fehler.hidden = false; return; }
        adresse.value = pr.adresse;
        const r = await api.koppeln(pr.adresse, $("#code").value, $("#name").value, $("#unsicher").checked);
        if (!r.ok) {
          fehler.textContent = r.text || t("f_" + r.fehler);
          if (r.fehler === "unverschluesselt") $("#unsicher-zeile").hidden = false;
          fehler.hidden = false; return;
        }
        $("#code").value = "";
        await api.hardware_pruefen();
        await neuLaden();
      } finally { beschaeftigt = false; $("#verbinden").disabled = false; }
    };
    $("#nochmal-pruefen").onclick = async () => { await api.hardware_pruefen(); neuLaden(); };
    $("#weiter-installieren").onclick = () => { schritt = 3; testWahl = false; prozessorWahl = false; darstellen(); };
    $("#prozessor-waehlen").onclick = () => { schritt = 3; testWahl = false; prozessorWahl = true; darstellen(); };
    $("#testmodus-waehlen").onclick = () => { schritt = 3; testWahl = true; prozessorWahl = false; darstellen(); };
    $("#installieren").onclick = async () => { await api.installieren(testmodus(), prozessorWahl); fertigZeigen = true; neuLaden(); };
    $("#nur-prozessor").addEventListener("change", async (e) => {
      await api.einstellung("geraet", e.target.checked ? "cpu" : "auto"); neuLaden();
    });
    $$("[data-regler]").forEach((r) => {
      const ein = r.querySelector("[data-regler-eingabe]");
      let zaehler = 0;
      ein.addEventListener("input", async () => {
        ein.dataset.zieht = "1";
        const nr = ++zaehler;
        reglerText(r, null);
        const p = await api.profil_vorschau(reglerWertMb(ein));
        if (nr === zaehler) reglerText(r, p);
      });
      ein.addEventListener("change", async () => {
        await api.einstellung("vram_grenze_mb", reglerWertMb(ein));
        delete ein.dataset.zieht;
        await neuLaden();
      });
    });
    $("#inst-nochmal").onclick = async () => { await api.installieren(testmodus()); neuLaden(); };
    $("#inst-abbrechen").onclick = () => api.installation_abbrechen();
    $("#los").onclick = async () => { fertigZeigen = false; schritt = null; await api.starten(); reiter = "status"; neuLaden(); };

    $$("[data-einstellung]").forEach((el) => {
      el.addEventListener("change", async () => {
        const wert = el.type === "checkbox" ? el.checked : el.value;
        const r = await api.einstellung(el.dataset.einstellung, wert);
        if (r && !r.ok && r.fehler) toast(r.fehler);
        if (el.dataset.einstellung === "sprache") { S.einstellungen.sprache = wert; spracheWaehlen(); }
        neuLaden();
      });
    });
    $("#ordner").onclick = () => api.ordner_oeffnen();
    $("#log-ordner").onclick = () => api.ordner_oeffnen();
    $("#verwaltung").onclick = () => api.verwaltung_oeffnen();
    $("#neu-koppeln").onclick = async () => { if (confirm(t("bestaetigen_koppeln"))) { await api.entkoppeln(); reiter = "status"; neuLaden(); } };
    $("#neu-installieren").onclick = async () => { await api.installieren(!!S.einstellungen.testmodus); reiter = "status"; zeigeReiter("status"); neuLaden(); };
    $("#entfernen").onclick = async () => { if (confirm(t("bestaetigen_entfernen"))) { await api.ki_entfernen(true); schritt = 2; reiter = "status"; neuLaden(); } };
    $("#beenden").onclick = () => api.beenden();
    $("#log-kopieren").onclick = () => {
      const text = $("#log").textContent;
      const fertig = () => toast(t("kopiert"));
      if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(fertig, alt); else alt();
      function alt() {
        const ta = document.createElement("textarea"); ta.value = text; document.body.append(ta); ta.select();
        document.execCommand("copy"); ta.remove(); fertig();
      }
    };
  }

  // ------------------------------------------------------------ Start
  async function start() {
    api = window.pywebview.api;
    uebersetzen();
    binden();
    await neuLaden();
    if (S && S.einstellungen.gekoppelt && !S.motor && !S.installation) schritt = 2;
    zeigeReiter(reiter);
    setInterval(neuLaden, 700);
    setInterval(() => logLaden(false), 2000);
  }
  if (window.pywebview && window.pywebview.api) start();
  else window.addEventListener("pywebviewready", start);
})();
