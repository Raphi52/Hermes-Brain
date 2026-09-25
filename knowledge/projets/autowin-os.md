---
type: domain
kind: system
scope: global
uid: projets/autowin-os
author_agent: claude
model: "claude-opus-5-5"
source: "session:conv-3"
created: 2026-09-25
status: active
confidence: confirmed
supersedes: []
tags: [projet, electron, typescript, agents]
---

# Autowin OS — cockpit d'orchestration d'agents

Projet dans `D:\Autowin`. Application **Electron + TypeScript** (electron-vite, electron-builder, tests Vitest) qui pilote des agents IA : conversations, pipeline scout → frame → terrain → build → clean → judge, skills dans `skills/`.

Elle embarque une copie du moteur du Brain dans `D:\Autowin\brain` et lit ce Brain personnel via le serveur local `127.0.0.1:8765` (configuration `%LOCALAPPDATA%\AmitelBrain\config.json`).
