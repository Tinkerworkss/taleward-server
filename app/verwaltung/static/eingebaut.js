// Verwaltung → Transkription: Schieberegler „Grafikspeicher für Taleward“ zeigt den Wert und das gewählte Modell an.
// Ohne JavaScript funktioniert das Formular genauso, nur ohne Live-Anzeige.
(function () {
  var regler = document.getElementById("vram-regler");
  var anzeige = document.getElementById("vram-anzeige");
  if (!regler || !anzeige) return;
  var stufen = JSON.parse(regler.dataset.stufen || "[]");  // [[ab MB, "Text"], …] absteigend
  var alles = regler.dataset.alles || "";
  function zeigen() {
    var mb = parseInt(regler.value, 10) || 0;
    var budget = mb === 0 ? parseInt(regler.max, 10) : mb;
    var text = stufen.length ? stufen[stufen.length - 1][1] : "";
    for (var i = 0; i < stufen.length; i++) { if (budget >= stufen[i][0]) { text = stufen[i][1]; break; } }
    anzeige.textContent = (mb === 0 ? alles : (mb / 1024).toFixed(1).replace(".", ",") + " GB") + " – " + text;
  }
  regler.addEventListener("input", zeigen);
  zeigen();
})();

// Verwaltung → Zusammenfassung: Anbieter-Auswahl zeigt den passenden Hinweis, trägt Adresse und empfohlenes Modell
// ein und blendet die Bestätigung nur ein, wenn sie nötig ist. Ohne JavaScript bleiben alle Hinweise aufklappbar.
(function () {
  var wahl = document.getElementById("anbieter-wahl");
  if (!wahl) return;
  var url = document.getElementById("anbieter-url");
  var modell = document.getElementById("anbieter-modell");
  var vorschlag = document.getElementById("anbieter-modell-vorschlaege");
  var wahlFeld = document.getElementById("modell-wahl-feld");
  var textFeld = document.getElementById("modell-text-feld");
  function fuellen(liste, sel, mitLeer, wert) {
    if (!sel) return;
    var leer = mitLeer ? sel.options[0] : null;
    while (sel.options.length) sel.remove(0);
    if (leer) sel.add(leer);
    for (var i = 0; i < liste.length; i++) sel.add(new Option(liste[i], liste[i]));
    sel.value = wert || "";
    if (sel.selectedIndex < 0) sel.selectedIndex = 0;
  }
  var bestaetigung = document.getElementById("anbieter-bestaetigung");
  var infos = document.querySelectorAll(".anbieter-info");
  var vorher = wahl.value;
  function zeigen(gewechselt) {
    var o = wahl.options[wahl.selectedIndex];
    for (var i = 0; i < infos.length; i++) { infos[i].open = infos[i].dataset.anbieter === wahl.value; }
    if (url) { url.disabled = wahl.value !== "andere"; if (gewechselt && wahl.value !== "andere") url.value = ""; }
    if (gewechselt) {
      var liste = [];
      try { liste = JSON.parse(o.dataset.modelle || "[]"); } catch (e) { liste = []; }
      fuellen(liste, modell, false, o.dataset.modell);
      fuellen(liste, vorschlag, true, o.dataset.vorschlaege);
    }
    var frei = wahl.value === "andere" && (!modell || modell.options.length === 0);
    if (wahlFeld) wahlFeld.hidden = frei;
    if (textFeld) textFeld.hidden = !frei;
    if (bestaetigung) bestaetigung.hidden = o.dataset.eu === "1";
  }
  wahl.addEventListener("change", function () { zeigen(wahl.value !== vorher); vorher = wahl.value; });
  zeigen(false);
})();
