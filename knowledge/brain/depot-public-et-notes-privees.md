---
type: decision
kind: decision
scope: global
uid: brain/depot-public-et-notes-privees
author_agent: claude
model: "claude-opus-5-5"
source: "url:https://github.com/Raphi52/Hermes-Brain"
created: 2026-09-25
status: active
confidence: confirmed
supersedes: []
tags: [brain, securite, vie-privee]
---

# Le dépôt est public : les notes privées vont dans knowledge/local/

`Raphi52/Hermes-Brain` est un dépôt GitHub **public** (API GitHub, `visibility: public`, lu le 2026-09-25). Tout ce qui est commité y est lisible par n'importe qui.

Règle : aucun secret, identifiant, adresse e-mail ni contenu personnel dans les notes commitées. Ce qui est privé s'écrit dans `knowledge/local/` : ce dossier est **indexé localement** comme le reste, mais `.gitignore` l'exclut, donc il ne quitte jamais la machine.
