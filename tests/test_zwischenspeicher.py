"""Schnittstellen-Antworten ohne Zwischenspeicher (Proxy/Browser ließen sonst frische Kampagnen verschwinden)."""



def test_schnittstelle_ohne_zwischenspeicher(client, world):
    """Alle Antworten unter /api/v1 tragen Cache-Control: no-store – außer Endpunkte, die bewusst anderes setzen."""
    from tests.test_step2a import API

    r = client.get(f"{API}/campaigns", headers=world["gm"])
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert client.get(f"{API}/health").headers["cache-control"] == "no-store"
    assert client.get(f"{API}/campaigns/gibt-es-nicht", headers=world["gm"]).headers["cache-control"] == "no-store"
    # Charakterbilder dürfen einen Tag im Speicher bleiben (setzt der Endpunkt selbst)
    c = client.get(f"{API}/campaigns", headers=world["gm"]).json()[0]
    me = next(m for m in client.get(f"{API}/campaigns/{c['id']}", headers=world["gm"]).json()["members"]
              if m["role"] == "gm")
    r = client.get(f"{API}/campaigns/{c['id']}/members/{me['id']}/portrait", headers=world["gm"])
    assert r.status_code in (200, 404)
    if r.status_code == 200:
        assert r.headers["cache-control"] == "private, max-age=86400"
    # Außerhalb der Schnittstelle unverändert
    assert "no-store" not in client.get("/downloads/").headers.get("cache-control", "")
