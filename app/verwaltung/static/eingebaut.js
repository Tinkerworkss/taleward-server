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
