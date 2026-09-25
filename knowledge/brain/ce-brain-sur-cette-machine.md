---
type: decision
kind: decision
scope: global
uid: brain/ce-brain-sur-cette-machine
author_agent: claude
model: "claude-opus-5-5"
source: "session:conv-3"
created: 2026-09-25
status: active
confidence: confirmed
supersedes: []
tags: [brain, installation, autowin]
---

# Ce Brain personnel remplace le Brain d'entreprise sur cette machine

Le Brain d'entreprise (un partage réseau interne, emplacement par défaut d'Autowin) **n'est pas joignable** depuis cette machine personnelle. Décision du 2026-09-25 : un Brain personnel séparé.

- **Corpus** : `C:\Users\User\Hermes-Brain` (le clone lui-même). Variable utilisateur `AMITEL_BRAIN_ROOT` qui pointe dessus.
- **Moteur** : copie locale dans `%LOCALAPPDATA%\AmitelBrain\tooling`, venv Python dans `%LOCALAPPDATA%\AmitelBrain\.venv`. Les noms « Amitel » sont gardés : c'est là qu'Autowin OS cherche le moteur.
- **Rappel automatique** : un hook `UserPromptSubmit` de Claude Code injecte les extraits pertinents à chaque prompt.
- **Mise à jour du corpus** : modifier `knowledge/`, puis relancer `install.ps1` (ou `brain_index.py`) pour réindexer.
