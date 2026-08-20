# Brief technique — SimsLink (gestionnaire de mods The Sims 4, Linux)

## 1. Contexte et objectif

**Nom du projet : SimsLink.** Application desktop Python (interface Flet, base SQLite) pour gérer les mods The Sims 4 sur Linux. Le manager officiel CurseForge n'existe pas sur Linux — ce projet comble ce vide, avec un différenciateur clé : diagnostic automatisé des crashs pour identifier le(s) mod(s) fautif(s).

Projet comparable identifié : [SimsForge](https://github.com/Teyk0o/simsforge) (stack TS/Express/Prisma, pas concurrent direct mais bonnes idées : import filesystem, détection de mods malveillants).

**Convention de langue du projet** : l'interface utilisateur est localisée (français/anglais, voir section 6.7), mais **le code source, tous les commentaires et le README doivent être rédigés en anglais**, sans exception — seule la couche de traduction UI contient du français.

---

## 2. Contraintes techniques du jeu (à respecter impérativement)

- **`.package`** : chargé par scan récursif complet — fonctionne à n'importe quelle profondeur sous `Mods/`.
- **`.ts4script`** : chargé uniquement à la racine de `Mods/` **ou** à un seul niveau de sous-dossier. Au-delà, il est ignoré par le jeu.
- **Règle d'installation retenue** : chaque mod obtient systématiquement un sous-dossier de premier niveau dédié : `Mods/<mod_id>/...`. Jamais à la racine directement (collisions de noms), jamais imbriqué davantage (casse les `.ts4script`).
- Le dossier `Mods/` du jeu reste **plat** (pas de classement multi-niveaux) — l'organisation riche vit dans la bibliothèque, reliée par symlinks.

---

## 3. Architecture

```
config.py       # chargement .env
db.py           # sqlite : schéma + migrations
curseforge.py   # client API : search, get_files, download (mode Direct uniquement)
download_watcher.py  # surveillance du dossier de téléchargement (mode Assisté)
mod_manager.py  # install / enable / disable / delete / update
dependencies.py # résolution graphe de dépendances (incl. détection de traductions)
package_parser.py  # lecture d'en-tête DBPF (.package) — ressources STBL, détection traduction
scanner.py      # scan incrémental (métadonnées) + scan complet (hash) à la demande
crash_analyzer.py  # parsing lastException.txt, bisection automatisée
i18n/           # fichiers de traduction UI (fr.json, en.json)
ui/
  - bibliotheque.py
  - catalogue.py
  - maj.py
  - mode_crash.py
  - parametres.py
```

Tous les fichiers ci-dessus (noms, code, commentaires) sont écrits en anglais dans l'implémentation réelle — les noms français utilisés dans ce brief ne servent qu'à la lisibilité du document de spec.

### Modèle : bibliothèque + symlinks
- **Bibliothèque** : dossier de stockage réel des mods installés (organisation simple, à plat — inutile de classer en multi-niveaux).
- **`Mods/<mod_id>/`** : symlink (ou copie si symlink impossible selon FS) pointant vers la bibliothèque.
- Activer/désactiver un mod = créer/supprimer le symlink. Rapide, réversible, aucun déplacement de fichiers.

---

## 4. Variables d'environnement (`.env`)

```
SIMS4_GAME_DIR=...        # dossier d'installation du jeu
SIMS4_MODS_DIR=...        # .../Documents/Electronic Arts/The Sims 4/Mods
SIMS4_USER_DIR=...        # .../Documents/Electronic Arts/The Sims 4
LIBRARY_DIR=...           # bibliothèque de mods gérée par l'appli
CURSEFORGE_API_KEY=...    # optionnelle — voir Mode Direct / Mode Assisté ci-dessous
DOWNLOAD_WATCH_DIR=...    # dossier de téléchargement surveillé (mode Assisté), défaut ~/Téléchargements
GAME_VERSION=...          # détectée auto si possible, sinon manuelle
```

---

## 4bis. Mode Direct vs Mode Assisté

La clé API CurseForge n'est **pas strictement bloquante pour démarrer le développement** (voir correction du point 0, section 8) : l'application fonctionne selon deux modes, avec un **bandeau permanent** indiquant l'état actif.

| Mode | Condition | Bandeau affiché |
|---|---|---|
| 🟢 **Mode Direct** | `CURSEFORGE_API_KEY` valide et configurée | "Mode Direct — connecté à CurseForge" |
| 🟡 **Mode Assisté** | Pas de clé, ou clé invalide/expirée | "Mode Assisté — installation via navigateur" |

### Mode Direct
Comportement déjà décrit en section 6.2/6.3 : catalogue interrogeable, badges de compatibilité automatiques, métadonnées (description/screenshots) récupérées via l'API, détection automatique des mises à jour disponibles.

### Mode Assisté (fonctionnement en l'absence de clé)

**Installation :**
1. L'utilisateur clique sur un mod (lien externe stocké, ou recherche manuelle) → `webbrowser.open(url_curseforge)`. Navigation humaine normale dans le navigateur — aucune requête automatisée vers CurseForge, donc aucun souci de CGU.
2. L'app surveille `DOWNLOAD_WATCH_DIR` (`download_watcher.py`, lib `watchdog`) et détecte l'apparition d'un nouveau fichier `.zip`/`.package`/`.ts4script`.
3. Confirmation utilisateur : *"Fichier détecté : `xxx.zip` — installer ce mod ?"*
4. Le fichier repasse dans le pipeline d'installation standard (extraction, détection profondeur `.ts4script`, placement `Mods/<mod_id>/`, ajout DB) — identique au mode Direct à partir de cette étape.
5. Le lien CurseForge d'origine est conservé en DB (`liens.curseforge_url`) même sans API, pour permettre la vérification manuelle ultérieure.

**Mises à jour ("Tout mettre à jour" en mode Assisté) :**
- Pas de comparaison automatique de version possible (aucune interrogation API).
- L'onglet Mises à jour devient une **liste de vérification manuelle** : chaque mod affiche un bouton "Vérifier sur CurseForge" → ouvre sa page stockée.
- Bouton **"Tout mettre à jour"** en mode Assisté : ouvre séquentiellement (ou en onglets groupés) les pages CurseForge de tous les mods installés ayant un lien connu, pour revue rapide par l'utilisateur.
- Si un nouveau fichier est téléchargé pour un mod déjà installé, le même `download_watcher.py` le détecte et propose : *"Nouveau fichier détecté pour `<mod existant>` — remplacer la version actuelle ?"* (reconnaissance par nom de fichier proche / confirmation manuelle si ambiguïté).
- Le remplacement déclenche : backup de l'ancienne version → extraction → placement → mise à jour de `version_installee` (valeur saisie/déduite du nom de fichier, pas de source API fiable).

**Perdu en mode Assisté (affiché clairement dans l'UI, pas caché) :**
- Pas de badge "mise à jour disponible" automatique.
- Pas de changelog automatique.
- Pas de vérification de compatibilité `game_version_min/max` avant re-téléchargement.

**Conservé intégralement, indépendant du mode :** bibliothèque, symlinks, dépendances (locales), Mode Crash, nettoyage cache, activer/désactiver/supprimer, scan incrémental.

---

## 5. Schéma SQLite

```sql
mods (
  id, curseforge_id, nom, auteur, categorie,
  chemin_lib, type_principal,        -- 'package' | 'script' | 'mixte'
  version_installee, version_latest,
  game_version_min, game_version_max,
  compat_status,                     -- 'compatible' | 'incompatible' | 'inconnue'
  distribution_tierce_autorisee,     -- bool, depuis l'API CurseForge
  actif, date_install, date_maj,
  vignette_url, vignette_locale,
  description_courte,                -- résumé (ex. summary de l'API)
  description_complete,              -- description longue (endpoint dédié de l'API)
  screenshots JSON,                  -- URLs des images additionnelles
  liens JSON                         -- site officiel, page CurseForge, donation, etc.
)

mod_files (
  id, mod_id, chemin_relatif, hash, extension
)

dependencies (
  id, mod_id, depend_de_curseforge_id,
  type_dependance,   -- 'requise' | 'optionnelle' | 'traduction'
  confiance,         -- 'confirmee' | 'suggeree' (pour les traductions auto-détectées)
  obligatoire
)

profils (id, nom)
profil_mods (profil_id, mod_id)

crash_log (
  id, date,
  lastexception_brut TEXT,
  mods_suspects_auto JSON,      -- mods identifiés via parsing traceback
  mods_actifs_snapshot JSON,
  dichotomie_en_cours BOOLEAN,
  dichotomie_historique JSON,   -- lots testés + résultats
  mod_confirme_fautif_id,
  note_utilisateur
)
```

---

## 6. Vues Flet

### 6.1 Bibliothèque
Grille de vignettes des mods installés (actifs + inactifs grisés). Chaque vignette affiche au minimum `description_courte`. Clic sur une vignette → **modal de détail** :
- description complète (`description_complete`)
- auteur, catégorie, versions (installée / dernière dispo), compat_status
- screenshots additionnels
- liens (page CurseForge, site de l'auteur)
- dépendances (liste + statut résolu/manquant, y compris traductions détectées — voir section 6.6)
- fichiers installés (chemins, hash)
- actions : activer/désactiver/supprimer

**Scan de démarrage — optimisation obligatoire (voir `scanner.py`) :**
- **Jamais de scan exhaustif bloquant au lancement.** L'UI affiche immédiatement l'état connu en DB (session précédente), sans écran d'attente.
- **Scan incrémental léger** en tâche de fond : comparaison taille + `mtime` par fichier (pas de lecture de contenu) contre `mod_files`. Seuls les fichiers nouveaux/modifiés déclenchent un hash complet. Un dossier de mod inchangé est ignoré entièrement.
- **Watcher temps réel** (`watchdog`, inotify Linux) sur `Mods/` pendant que l'app tourne : détecte les ajouts/suppressions manuels sans attendre le prochain lancement.
- **Bouton "Scan complet"** dans Paramètres, déclenché manuellement uniquement (recalcul de tous les hash — utile après modification externe suspecte, ou pour la détection de doublons/conflits). Avertissement de durée affiché avant lancement sur grosse bibliothèque.
- Scan complet parallélisé (`concurrent.futures`/`multiprocessing`) — le hashing est CPU-bound, se prête bien à la parallélisation multi-cœurs.

### 6.2 Catalogue CurseForge
Recherche/parcours filtré sur `GAME_VERSION`. Badge de compatibilité sur chaque vignette :
- 🟢 compatible
- 🔴 incompatible
- ⚪ "compatibilité inconnue" (mods sans info claire de version sur CurseForge — affichés, pas masqués)

Chaque vignette affiche `description_courte`. Clic sur la vignette (hors zone du bouton Installer) → même modal de détail que la bibliothèque (description complète, screenshots, auteur, versions, dépendances requises), avant décision d'installation.

Bouton **Installer** directement sur la vignette. Si `distribution_tierce_autorisee = false` → le bouton devient **"Ouvrir sur CurseForge"** (lien externe, pas de download programmatique possible).

Installation : téléchargement → détection profondeur `.ts4script` → placement `Mods/<mod_id>/` → résolution dépendances → ajout DB (y compris récupération et stockage des métadonnées : description, screenshots, liens).

### 6.3 Mises à jour disponibles

**En Mode Direct :** mods où `version_latest ≠ version_installee`, changelog si fourni par l'API. Bouton **"Tout mettre à jour"** : met à jour en lot tous les mods détectés obsolètes, avec dry-run préalable (aperçu de la liste + changelogs avant confirmation) et backup automatique par mod avant remplacement. Mise à jour unitaire également disponible.

**En Mode Assisté :** voir section 4bis — le bouton "Tout mettre à jour" ouvre les pages CurseForge des mods installés pour revue manuelle, le remplacement effectif se fait via détection du dossier de téléchargement.

### 6.6 Détection de mods de traduction

Objectif : repérer automatiquement qu'un mod est une traduction d'un autre mod déjà en bibliothèque, et le relier comme dépendance de type `traduction` plutôt que de le traiter comme un mod indépendant sans contexte.

**Limite de départ :** l'API CurseForge n'a pas de type de relation "traduction" dédié — seulement `embeddedLibrary` / `incompatible` / `optionalDependency` / `requiredDependency`. Aucune méthode n'est fiable seule ; combinaison de signaux nécessaire, jamais d'association automatique silencieuse.

**Méthodes, par ordre de coût croissant :**
1. **Parsing de la description** (signal le plus fiable) : recherche de mots-clés multilingues ("traduction", "translation", "übersetzung", "traducción") + extraction d'URL CurseForge pointant vers un mod déjà en bibliothèque.
2. **Heuristique sur le nom/slug** (signal faible, filtre de pré-sélection uniquement) : motifs `[FR]`, `- French Translation`, `_VF`, `(Traduction)`.
3. **Analyse du `.package` via `package_parser.py`** (confirmation technique, à la demande seulement — pas en scan systématique) : lecture d'en-tête DBPF pour vérifier que le fichier ne contient que des ressources STBL (aucun `.ts4script`, taille anormalement faible), puis comparaison des Group ID/Instance ID avec les STBL du mod source candidat — une correspondance confirme quasi certainement le lien.

**Comportement UI :** toute association proposée (quelle que soit la méthode) est soumise à confirmation utilisateur, jamais appliquée silencieusement. Le champ `confiance` (`confirmee`/`suggeree`) distingue les liens validés manuellement des suggestions automatiques.

### 6.4 Mode Crash

**Bouton "J'ai crashé"** en accès permanent (barre du haut).

**Étape 1 — Analyse automatique**
- Lecture de `lastException.txt` (ignorer `lastCrash.txt`, illisible et inexploitable même par la communauté).
- Parsing des lignes `File "..."` de la traceback.
- Cross-référencement avec `mod_files` : toute ligne sous `Mods/` → mod identifié directement comme suspect n°1.
- Reconnaissance de patterns connus par regex (ex. erreurs d'import de librairies obsolètes type sims4communitylib/ts4lib) pour les cas où le mod n'apparaît pas directement dans la trace.
- Résultat affiché : liste de mods suspects, classés par confiance, avec la ligne de trace correspondante.
- **Ne jamais proposer de suppression automatique** sur la base d'une seule occurrence — une LastException isolée peut être bénigne et sans rapport avec un mod cassé.

**Étape 2 — Bisection automatisée (si pas de suspect clair)**
- Implémentation logicielle de la méthode 50/50 communautaire : désactivation par lots (via symlinks, pas de déplacement physique), confirmation utilisateur après relance du jeu, convergence en O(log n) itérations.
- Historique des lots testés stocké dans `dichotomie_historique`.

**Étape 3 — Actions rapides**
- Bouton "Ouvrir le dossier" par mod suspect (ouvre `Mods/<mod_id>/` dans le gestionnaire de fichiers).
- Comparaison avec le snapshot du crash précédent pour repérer ce qui a changé.
- Désactivation groupée "mods ajoutés/modifiés dans les 7 derniers jours".
- Historique des crashs par mod pour repérer un pattern récurrent.

---

### 6.5 Nettoyage du cache

Bouton **"Vider le cache"**, accessible depuis Paramètres et depuis le Mode Crash (le cache corrompu après une mise à jour de mods est une cause de crash récurrente et bien documentée).

**Éléments ciblés, avec justification :**

| Élément | Action | Pourquoi |
|---|---|---|
| `localthumbcache.package` | Supprimer le fichier | <cite index="49-1">Fichier de cache principal à vider régulièrement, et systématiquement après ajout/suppression de mods.</cite> Contient des données de thumbnails/tuning qui peuvent référencer d'anciens mods retirés. Si un mod est mis à jour sans vider ce cache, <cite index="56-1">le cache garde les données de l'ancienne version, ce qui peut provoquer des lookups invalides et un crash.</cite> |
| `cache/` (contenu) | Vider le contenu, garder le dossier et le fichier `FileCache.cfg`/`.ini` s'il existe | <cite index="41-1">Dossier de données temporaires régénéré automatiquement par le jeu.</cite> |
| `cachestr/` (contenu) | Vider le contenu, garder le dossier | <cite index="43-1">Cache régénéré par le jeu, à vider périodiquement.</cite> |
| `cachewebkit/` | Supprimer si présent | <cite index="49-1">N'existe que pendant l'exécution du jeu ; s'il persiste après un crash, il peut être supprimé sans risque.</cite> |
| `onlinethumbnailcache/` | Supprimer si présent | <cite index="43-1">Cache des vignettes en ligne (galerie), suppression optionnelle mais parfois utile.</cite> |
| `localsimtexturecache.package` | Supprimer si présent | <cite index="42-1">Cache des textures de Sims, plafonné à 100 Mo, utile à vider en cas de problèmes d'affichage des personnages.</cite> |

**Ne jamais toucher** : saves, tray files, screenshots, `options.ini`, `resource.cfg`.

**Comportement :**
- Confirmation avant suppression (aucune perte de données de jeu, mais bon usage de prévenir).
- Suggestion automatique du nettoyage après toute installation/désactivation/mise à jour de mod, avec rappel explicite : <cite index="54-1">un cache non vidé après un changement de mods peut lui-même devenir la source du crash, indépendamment de la qualité du mod.</cite>
- `lastException.txt`/`lastCrash.txt` ne sont **pas** inclus dans ce nettoyage global — ils restent gérés séparément par le Mode Crash (voir 6.4), pour ne pas perdre l'historique de diagnostic avant analyse.

---

### 6.7 Localisation

- Langues supportées au lancement : **français** et **anglais**.
- Détection automatique de la langue système au premier démarrage, avec sélecteur manuel de override dans Paramètres (Français / Anglais / Système).
- Fichiers de traduction dans `i18n/fr.json` et `i18n/en.json` — clés en anglais, aucune chaîne UI codée en dur dans le code applicatif.
- Rappel : cette localisation ne concerne que l'interface utilisateur. Code source, commentaires et README restent intégralement en anglais (voir section 1).

### 6.8 Paramètres (vue `parametres.py`)

| Catégorie | Paramètres |
|---|---|
| **Dossiers** | Dossier de jeu, dossier Mods, dossier utilisateur Sims4, dossier bibliothèque, dossier de téléchargement surveillé (mode Assisté) |
| **CurseForge** | Clé API (champ + indicateur de validité), version de jeu ciblée (auto-détectée, éditable) |
| **Langue** | Français / Anglais / Système (auto) |
| **Scan** | Fréquence du scan léger (à chaque démarrage / manuel uniquement), activer/désactiver le watcher temps réel |
| **Cache** | Suggestion automatique de nettoyage après changement de mods (on/off) |
| **Sauvegardes** | Nombre de backups conservés avant purge, dossier de destination des backups |
| **Mode Crash** | Durée de conservation de l'historique `lastException`, sensibilité de la reconnaissance de patterns |
| **Interface** | Thème clair/sombre, taille des vignettes (grille compacte/large) |
| **Avancé** | Niveau de log (info/debug), méthode de liaison (symlink / copie si le FS ne supporte pas les symlinks) |

---

## 7. Features transverses

- Détection au premier lancement des mods déjà présents hors gestionnaire (scan + hash) → import dans la bibliothèque.
- Backup automatique avant update/delete, avec rollback.
- Détection de doublons/conflits de fichiers (hash sur `.package`, comparaison des `.ts4script` par nom si possible).
- Vérification que `Script Mods Allowed` est activé dans `options.ini`.
- Profils de mods activables en un clic (ex. "build only" vs "gameplay complet").
- Mode dry-run avant mise à jour groupée + changelog affiché.
- Liste noire locale éditable pour mods suspects (version simplifiée du système de signalement de SimsForge).

---

## 8. Points de vigilance identifiés en recherche

0. **Clé API CurseForge : prérequis pour le Mode Direct uniquement, pas un blocage absolu du projet.** L'accès à l'API se fait exclusivement via une clé unique délivrée après candidature soumise sur [console.curseforge.com](https://console.curseforge.com/), examinée manuellement par l'équipe Overwolf (taux d'approbation historique élevé, ~88%, gestionnaires de mods = cas d'usage le plus courant et le mieux accueilli) — il n'existe aucune alternative par connexion OAuth avec un compte utilisateur CurseForge, et le scraping est explicitement interdit par les CGU (risque de ban, sans même résoudre le problème puisque le CDN exige aussi une clé depuis le 16 juillet 2026). En revanche, le développement n'est **pas bloqué en attendant la clé** : le Mode Assisté (section 4bis) permet de construire et tester bibliothèque, symlinks, dépendances, Mode Crash, nettoyage cache, scan incrémental et installation via surveillance du dossier de téléchargement — tout sauf `curseforge.py` lui-même. La demande de clé doit néanmoins être soumise dès que possible en parallèle, pour ne pas retarder le passage en Mode Direct une fois le reste du projet mature.

1. **API CurseForge** : même avec une clé approuvée, certains créateurs désactivent la distribution tierce sur leurs mods spécifiquement (`distribution_tierce_autorisee = false`) — fallback obligatoire vers lien externe.
2. **`lastCrash.txt` inexploitable** — ne pas investir de temps de parsing dessus, se concentrer sur `lastException.txt`.
3. **LastException != mod cassé à coup sûr** — beaucoup sont circonstancielles et sans conséquence. Le diagnostic doit rester une aide à la décision, jamais une automatisation destructive.
4. **Filtres et déduplication absents côté CurseForge officiel** — confirme l'intérêt de nos features de détection de doublons/conflits comme différenciateur.
