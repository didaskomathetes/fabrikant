# ADR 0046: OIDC-Login (generischer Client, optionales Extra `fabrikant[oidc]`)

**Status:** Akzeptiert (2026-11-13)
**Betrifft:** Auth-Modi aus ADR 0030 (dort als Folge-Issue #160 vorgesehen), Prinzip „Stdlib + PyYAML“ (Issue #160).

## Kontext

In Firmen läuft die Anmeldung über einen zentralen IdP (Entra ID, Okta, Keycloak, AWS Cognito).
Eigene Passwörter pro Tool sind dort meist nicht erlaubt. Alle genannten IdPs sprechen OpenID Connect.

## Entscheidung

- **Ein generischer OIDC-Client** statt einzelner Integrationen (`FABRIKANT_AUTH=oidc`, Modul `oidc.py`).
  Cognito & Co. werden nur als Konfigurationsbeispiele dokumentiert.
- **Ablauf:** Authorization Code Flow mit PKCE (S256), `state` (einmalig, TTL 10 min, max. 1000 offene
  Versuche) und `nonce`. Discovery über `<issuer>/.well-known/openid-configuration`; der `issuer` im
  Dokument muss exakt zur Konfiguration passen. Token-Austausch per `client_secret_post`.
  Issuer und IdP-Endpunkte müssen `https` sein (`http` nur für Loopback, z. B. lokaler Test-IdP).
- **ID-Token-Prüfung:** Signatur per JWKS und `iss`, `aud`, `exp`, `iat`, `sub` Pflicht, `nonce`
  gegen den gespeicherten Wert. Nur asymmetrische Algorithmen (RS/PS/ES); `none`/HS* sind ausgeschlossen.
- **Rollen-Mapping:** `FABRIKANT_OIDC_ROLE_MAP="gruppe=rolle,…"` auf die Rollen aus ADR 0030, Claim per
  `FABRIKANT_OIDC_GROUPS_CLAIM` (Default `groups`; Cognito: `cognito:groups`). Bei mehreren Treffern gewinnt
  die höchste Rolle. **Ohne gemappte Rolle: kein Login** (403, keine Session). Das Mapping wird einmalig
  beim Login ausgewertet; die Rolle ist in der Session fixiert (Änderungen am IdP wirken ab dem nächsten Login).
- **Sessions:** Wie im Modus `local` (Cookie, CSRF, TTL). OIDC-Sessions tragen ihre Rolle selbst und
  hängen nicht an `state/users.json`. Der Benutzername kommt aus `preferred_username`, `email` oder `sub`.
  `FABRIKANT_TOKEN` bleibt unverändert als `admin`-API-Token. Kein Refresh-Token, kein Single Logout.
- **Abhängigkeit (Abweichung von „Stdlib + PyYAML“):** Signaturprüfung braucht Krypto (RSA/ECDSA), die die
  Stdlib nicht bietet; eine eigene Implementierung wäre ein Sicherheitsrisiko. Daher das **optionale Extra
  `fabrikant[oidc]`** (`pyjwt[crypto]`). Ohne Extra bricht `fabrikant serve` im Modus `oidc` mit einer klaren
  Meldung ab; alle anderen Modi bleiben abhängigkeitsfrei.
- **Routes:** `GET /api/login/oidc` (Redirect zum IdP) und `GET /api/oidc/callback` sind öffentlich; die
  UI leitet bei fehlender Session direkt zum IdP.

## Konsequenzen

- ADR 0030 nennt `oidc` noch als abgelehnten Wert; dieser ADR löst das ein (ADR 0030 bleibt unverändert).
- Der Session-Cookie ist `SameSite=Strict`; er wird nach dem IdP-Redirect gesetzt, API-Aufrufe der UI
  sind same-site und enthalten ihn.
- Der Rollenverlust am IdP wirkt erst nach Session-Ablauf (`FABRIKANT_SESSION_TTL`); kurze TTL wählen, wenn nötig.
- Entra ID liefert Gruppen als Objekt-IDs (GUIDs) → diese im Mapping verwenden.
