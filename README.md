# Hermes Brain

Rappel automatique d’un corpus Markdown partagé dans **Hermes Agent**, **Claude Code** et **Codex CLI**, sans envoyer les embeddings à une API.

Le moteur :

- indexe localement `knowledge/**/*.md` avec FastEmbed ;
- combine recherche dense et BM25 ;
- injecte seulement quelques extraits pertinents et bornés ;
- conserve la provenance ;
- échoue silencieusement si le brain est indisponible ;
- propose les nouvelles connaissances dans `inbox/` avant revue humaine.

> Ce dépôt public contient le moteur et des templates, **aucune connaissance Amitel interne**.

## Installation Windows — une commande

Ouvrez PowerShell dans le clone :

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\install.ps1
```

Par défaut, le clone lui-même devient le brain. Pour connecter un brain partagé :

```powershell
.\install.ps1 -BrainRoot "//serveur/partage/Mon Brain"
```

L’installateur :

1. crée `%LOCALAPPDATA%\AmitelBrain\.venv` ;
2. installe les dépendances locales ;
3. copie une liste fermée de modules runtime dans `%LOCALAPPDATA%\AmitelBrain\tooling` ;
4. construit un index transactionnel ;
5. ajoute sans écraser un hook `UserPromptSubmit` à Claude Code et Codex ;
6. demande au `codex app-server` local le hash du handler de commande exacte, l’approuve et vérifie son état `trusted` ;
7. installe et active le plugin Hermes ;
8. sauvegarde chaque configuration modifiée.

Le support Codex nécessite une version récente où la feature `hooks` est stable. L’installation s’arrête avec une erreur explicite si le Codex local ne sait pas découvrir ou approuver ce handler.

Redémarrez ensuite Hermes, Claude Code et Codex. Les nouveaux prompts reçoivent automatiquement les extraits pertinents.

## Brain d’équipe

Pour connecter un partage interne, utilisez son chemin fourni par votre équipe :

```powershell
.\install.ps1 -BrainRoot "//serveur/partage/Brain équipe"
```

Le partage fournit uniquement les **données** (`knowledge/`, `inbox/` et `tooling/index/`). Aucun Python n’y est chargé à l’exécution : les hooks et le service utilisent la copie locale installée depuis le clone. Pour mettre le runtime à jour, mettez à jour un clone de confiance puis relancez `install.ps1`.

## Désinstallation

```powershell
.\uninstall.ps1
```

Le désinstalleur retire seulement les hooks et le plugin Hermes Brain. Il ne supprime ni le corpus ni les sauvegardes.

## Utilisation manuelle

```powershell
$python = "$env:LOCALAPPDATA\AmitelBrain\.venv\Scripts\python.exe"
$oldPythonPath = $env:PYTHONPATH
Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
& $python tooling/brain_index.py --knowledge knowledge --out tooling/index
& $python tooling/brain_query.py --index tooling/index --q "ma question" --k 5
if ($null -ne $oldPythonPath) { $env:PYTHONPATH = $oldPythonPath }
```

## Écriture et curation

Une IA ne modifie pas directement `knowledge/`. Elle dépose une candidate :

```powershell
& "$env:LOCALAPPDATA\AmitelBrain\.venv\Scripts\python.exe" tooling/brain_propose.py `
  --inbox inbox --title "Décision" --body "..." --type decision `
  --scope global --author-agent hermes --model "modele-utilise" `
  --source "ticket:PROJET-123"
```

Relisez, corrigez et promouvez ensuite la note vers `knowledge/` via Git.

## Sécurité

- Le service écoute uniquement sur `127.0.0.1`.
- Requêtes et réponses sont authentifiées par jeton local et HMAC.
- Sous Windows, le jeton reçoit une ACL limitée au compte courant.
- Le runtime Python exécuté automatiquement est local ; un contributeur du corpus partagé ne peut pas le remplacer via le partage.
- L’installateur n’approuve pas globalement les hooks Codex : il enregistre uniquement le hash retourné pour la commande exacte ajoutée à `~/.codex/hooks.json`.
- Les chemins de notes et de candidates sont contrôlés sur le descripteur réellement ouvert.
- Les candidats contenant des secrets ou PII évidents sont rejetés.

Limites : le scan PII est heuristique et un processus hostile exécuté sous le même compte Windows partage le même périmètre de confiance.

## Tests

```bash
python -m unittest discover -s tooling/tests -v
```

## Structure

```text
knowledge/                         corpus Markdown versionné
inbox/                             candidates non actives
integrations/hermes-amitel-brain/  plugin Hermes
integrations/windows/              adaptateur Claude/Codex
tooling/                           index, retrieval, service et tests
```

## Licence

MIT — voir [LICENSE](LICENSE).
