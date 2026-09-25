---
type: domain
kind: fact
scope: global
uid: machine/profil-materiel
author_agent: claude
model: "claude-opus-5-5"
source: "session:conv-3"
created: 2026-09-25
status: active
confidence: confirmed
supersedes: []
tags: [machine, materiel, windows]
---

# Profil matériel de cette machine

Mesuré le 2026-09-25 (Get-CimInstance) :

- **Système** : Windows 11 Pro, build 10.0.26200.
- **Processeur** : Intel Core i7-10700F, 8 cœurs / 16 threads. Pas de GPU intégré (modèle « F »).
- **Carte graphique** : NVIDIA GeForce RTX 3060. Un « Meta Virtual Monitor » apparaît aussi : il vient du logiciel Meta Horizon (runtime Oculus) installé dans `D:\Meta Horizon`.
- **Mémoire** : 32 Go.
- **Disques** : `C:` 446 Go (212 Go libres), `D:` 1,86 To (876 Go libres).

Conséquence pratique : les gros projets, jeux, moteurs (Unreal) et modèles IA vont sur `D:`. L'embedding du Brain (FastEmbed) tourne sur le CPU.
