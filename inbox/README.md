# Inbox — propositions des IA

Les IA écrivent ici des **candidates**, jamais directement dans `knowledge/`.

## Flux

1. `tooling/brain_propose.py` crée une note immuable `status: candidate` avec provenance.
2. Validation : source vérifiable, absence de secret/PII, portée correcte, pas de doublon.
3. Revue humaine ou curator autorisé.
4. Promotion vers `knowledge/<type>/` avec `status: active`.
5. Réindexation locale puis commit/push.

Une candidate rejetée est supprimée dans une PR dédiée ou archivée selon la politique du repo.
Ne jamais modifier silencieusement une candidate déjà revue : en créer une nouvelle.
