# Hermes Brain — second brain personnel

Mémoire personnelle d'une machine de développement Windows, rappelée automatiquement dans **Claude Code**, **Autowin OS**, **Hermes Agent** et **Codex CLI**. Tout reste local : l'indexation et les embeddings tournent sur la machine, rien n'est envoyé à une API.

Ce dépôt contient **le moteur** et **le corpus** (`knowledge/`). Il est public : les notes privées vivent dans `knowledge/local/`, indexé localement mais exclu par `.gitignore`.

## Ce que contient le corpus

```text
knowledge/
  machine/       profil matériel, outils installés ou absents, hooks Claude Code
  projets/       carte des projets de développement et de leurs chemins
  brain/         comment ce Brain est installé ici, règle dépôt public / notes privées
  preferences/   langue et style de réponse attendus
  local/         notes privées — jamais commitées
  _TEMPLATE.md   modèle d'une note
inbox/           propositions des IA, en attente de revue
```

Une note = une idée, un en-tête `type / scope / source / created / status`, un nom de fichier court en kebab-case. Une correction n'écrase pas : elle crée une nouvelle note avec `supersedes: [[ancienne-note]]`.

## Le moteur

- indexe `knowledge/**/*.md` avec FastEmbed (dense) + BM25 ;
- injecte à chaque prompt quelques extraits pertinents et bornés, avec leur provenance ;
- échoue en silence si le Brain est indisponible ;
- reçoit les nouvelles connaissances dans `inbox/` avant revue humaine (`brain_propose.py`).

Provenance du code : copie embarquée d'Autowin OS (`D:\Autowin\brain`, commit `46b6261d`), elle-même union de la PR #1 de ce dépôt et de la copie de travail `brain-tooling`. Deux écarts avec cette source : l'installateur retombe sur le Python 3 le plus récent quand 3.11 est absent, et le jeu de questions d'évaluation `tooling/eval/rag-golden.json` (propre au corpus d'entreprise) n'est pas repris.

## Installation Windows — une commande

Dans PowerShell, depuis le clone :

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\install.ps1
```

Le clone lui-même devient le Brain. L'installateur :

1. crée `%LOCALAPPDATA%\AmitelBrain\.venv` (uv, sinon `py -3.11`, sinon `py -3` ; Python 3.11 minimum) ;
2. installe les dépendances de `tooling/requirements.txt` ;
3. copie les modules d'exécution dans `%LOCALAPPDATA%\AmitelBrain\tooling` ;
4. pose `AMITEL_BRAIN_ROOT`, `AMITEL_BRAIN_CODE_ROOT` et `AMITEL_BRAIN_PYTHON` (variables utilisateur) ;
5. ajoute, sans écraser les autres, un hook `UserPromptSubmit` à Claude Code et Codex ;
6. copie le plugin Hermes et l'active si la commande `hermes` existe ;
7. construit l'index dans `tooling/index/` ;
8. sauvegarde chaque configuration modifiée (`*.amitel-brain.<date>.bak`).

Les noms « AmitelBrain » sont conservés : c'est là qu'Autowin OS cherche le moteur et sa configuration. Redémarrez ensuite Claude Code, Autowin OS, Hermes et Codex.

## Ajouter ou corriger une connaissance

1. Écrire la note dans `knowledge/<dossier>/` (ou `knowledge/local/` si elle est privée), en partant de `_TEMPLATE.md`.
2. Réindexer :

```powershell
$python = "$env:LOCALAPPDATA\AmitelBrain\.venv\Scripts\python.exe"
& $python tooling/brain_index.py --knowledge knowledge --out tooling/index
& $python tooling/brain_query.py --index tooling/index --q "ma question" --k 5
```

3. Commiter les notes publiques. `knowledge/local/` ne part jamais.

Une IA ne modifie pas `knowledge/` directement : elle dépose une candidate dans `inbox/` avec `brain_propose.py`, et un humain la promeut.

## Désinstallation

```powershell
.\uninstall.ps1
```

Retire les hooks et le plugin. Le corpus et les sauvegardes restent.

## Sécurité

- Le service n'écoute que sur `127.0.0.1`, requêtes authentifiées par jeton local et HMAC ; le jeton a une ACL limitée au compte courant.
- Le code Python exécuté automatiquement est la copie locale installée, jamais celle d'un partage.
- Les candidates contenant des secrets ou des données personnelles évidentes sont rejetées (détection heuristique).
- **Dépôt public** : pas de secret, d'identifiant ni de contenu personnel dans une note commitée.

## Tests

```bash
python -m unittest discover -s tooling/tests -v
```

**État au 2026-09-25 : 120 tests, 8 rouges hérités** (3 échecs, 5 erreurs), identiques dans la copie source `D:\Autowin\brain` : ce changement n'en a ajouté aucun. Les tests sont restés sur d'anciennes versions du code :

- 6 visent des API renommées ou élargies (`LocalThreadingHTTPServer` devenu `run_server` — 2 tests —, argument `threads` de l'embedding, liste de modules du plugin sans `brain_singleton`, signature de format de l'index, exemple de source `email:` désormais valide) ;
- 2 décrivent un **comportement retiré** : `/health` n'indique plus quel corpus le serveur sert, `/shutdown` n'existe plus, et le hook ne redémarre plus un serveur resté sur un ancien corpus. Conséquence pratique : après un changement de `-BrainRoot`, arrêter le `brain_server` en cours avant de relancer.

À corriger dans la copie source, puis à resynchroniser ici, pour ne pas faire diverger les deux copies.

## Licence

MIT — voir [LICENSE](LICENSE).
