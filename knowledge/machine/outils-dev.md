---
type: domain
kind: fact
scope: global
uid: machine/outils-dev
author_agent: claude
model: "claude-opus-5-5"
source: "session:conv-3"
created: 2026-09-25
status: active
confidence: confirmed
supersedes: []
tags: [machine, outils, toolchain]
---

# Outils de développement installés (et absents)

**Absents** (vérifié le 2026-09-25) : **GitHub CLI `gh`**, **uv**, Docker, Rust, Go, Codex CLI, Hermes CLI. La commande `dotnet` existe mais ne se charge pas.

Conséquences pour un agent : passer par `git` et l'API GitHub (curl) au lieu de `gh` ; l'installateur du Brain crée son venv avec `py -3`, pas avec uv.

**Installés**, versions lues le 2026-09-25 :

- **Node.js** 20.15.1, **npm** 10.7.0.
- **Python** 3.12.4 seulement, via le lanceur `py` (Python install manager). **Aucun Python 3.11** : `py -3.11` échoue, `py -3` donne 3.12.
- **Git** 2.51 pour Windows, identifiants via `credential.helper=wincred`. Git Bash est disponible.
- **Claude Code** 2.1.282, **VS Code** 1.107.1.
- **Java** 8 (1.8.0_481), **Chocolatey** 2.3.0, **ffmpeg** 8.0.1 (paquet winget Gyan), **adb** 1.0.41 (Android SDK platform-tools).
