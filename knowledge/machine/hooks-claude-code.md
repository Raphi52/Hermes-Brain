---
type: domain
kind: fact
scope: global
uid: machine/hooks-claude-code
author_agent: claude
model: "claude-opus-5-5"
source: "session:conv-3"
created: 2026-09-25
status: active
confidence: confirmed
supersedes: []
tags: [claude-code, hooks, configuration]
---

# Hooks Claude Code installés sur cette machine

Lus dans `~/.claude/settings.json` le 2026-09-25 (rôles tirés de leurs libellés `statusMessage`), scripts PowerShell sous `~/.claude/hooks/` :

- **UserPromptSubmit** : `thinking-mode.ps1`, `session-inject.ps1` (injecte l'identifiant de session), `advisory-guard.ps1` (répondre directement plutôt que lancer un pipeline), `full-autonomy-directive.ps1` (actif seulement avec `AUTOWIN_AUTONOMY=1`), `git-auth-gate.ps1` (autorise commit/push seulement si l'utilisateur l'a dit).
- **PreCompact** : `precompact-runcheck.ps1` (signale un RUN.md encore ouvert).

L'installateur de ce Brain **ajoute** son propre hook `UserPromptSubmit` sans toucher aux autres, et sauvegarde le fichier avant de l'écrire (`settings.json.amitel-brain.<date>.bak`).
