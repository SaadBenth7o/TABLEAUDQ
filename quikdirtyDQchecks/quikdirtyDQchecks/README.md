# Dremio DQ POC — Data Quality Checks

Framework de **Data Quality** (DQ) pour automatiser les vérifications de qualité des données sur les tables VIRTUALISATION de Dremio.

---

## Vue d'ensemble

Ce projet est un **POC (Proof of Concept)** qui commence par couvrir les vérifications de **Completude**, mais est conçu pour supporter d'autres règles DQ au fur et à mesure :

- ✅ **Completude** — Vérifier les valeurs non-null et non-vides (actuel)
- ⏳ Unicité, Conformité patterns, Validité métier, etc. (futur)

**Scope actuel** : Vérifications sur **données des tiers** (CIHOne.CLIENTS) mappées en VIRTUALISATION.

### ⚠️ Colonnes KYC et CSP

Les colonnes sensibles au **Know Your Customer (KYC)** et aux **Catégories Socio-Professionnelles (CSP)** nécessitent une attention particulière — impacts réglementaires directs sur la conformité.

---

## Scores & Flags

| Score | Flag | Signification |
|-------|------|---|
| ≥ 90% | **PASS** ✓ | Excellente qualité |
| ≥ 70% | **WARN** ⚠ | À surveiller |
| < 70% | **FAIL** ✗ | Problème sérieux |
| N/A   | **ERROR** ? | Requête échouée (Dremio indisponible) |

---

## Installation

### Prérequis

- Python 3.11+
- Accès réseau à Dremio (environnement corporate)

### 1. Cloner le repo

```bash
git clone https://github.com/SaadBenth7o/quikdirtyDQchecks.git
cd quikdirtyDQchecks
```

> ⚠️ **Important** : Ne pas télécharger le ZIP. Utiliser `git clone` pour éviter le sous-dossier parasite `quikdirtyDQchecks-main/`.

### 2. Dépendances

```bash
pip install -r requirements.txt
```

### 3. Configuration Dremio

Créer un fichier `.env` à la racine du projet (copier depuis `.env.example`) :

```env
DREMIO_HOST=http://dlakegtwprd:9047
DREMIO_AUTH_TYPE=bearer
DREMIO_API_KEY=<votre_clé_API>
SCORE_PASS_THRESHOLD=90
SCORE_WARN_THRESHOLD=70
EXCEL_FILE=Stewardship_Workshop_Template_Tiers_Counterparties_fixed.xlsx
EXCEL_SHEET=Quality_checks_poc
DREMIO_PUBLISH_AUTO=true
DREMIO_PUBLISH_HOME_NAME=@S141
DREMIO_PUBLISH_DATASET_NAME=TiersDataQualityReport.all_columns_history
```

Si ces trois variables de publication sont définies, `lib/consolidate.py` pousse automatiquement le CSV consolidé dans l'espace perso Dremio après chaque exécution, en réutilisant le chemin cible et en écrasant le contenu via le flow d'upload Dremio.

### 4. Générer la configuration

La première fois (ou après modification de l'Excel) :

```bash
python lib/run_dq.py --refresh-config
```

---

## Utilisation

### Run simple (config déjà générée)

```bash
python lib/run_dq.py
```

### Régénérer la config depuis l'Excel + lancer les checks

```bash
python lib/run_dq.py --refresh-config --run
```

### Consolider les résultats du jour pour le reporting

```bash
python lib/consolidate.py
```

Si la publication Dremio est configurée dans `.env`, ce script met aussi à jour le dataset perso cible à chaque lancement.

### Migrer tout l'historique existant (première fois uniquement)

```bash
python lib/consolidate.py --all
```

### Sans interface (mode scripts)

Le projet fonctionne directement avec les scripts Python dans `lib/`.

- Checks DQ: `python lib/run_dq.py`
- Consolidation: `python lib/consolidate.py`
- Consolidation historique complete: `python lib/consolidate.py --all`

---

## Pipeline d'exécution

Le workflow complet chaîne 4 étapes automatisées :

### Étape 1 : Lecture Excel → Configuration

```bash
python lib/run_dq.py --refresh-config
```

- **excel_parser.py** lit la feuille `Quality_checks_poc`
- Filtre les lignes contenant `VIRTUALISATION` dans la colonne SQL
- Crée un **query_id** unique (SHA-256 premiers 12 chars) par requête SQL
- Déduplique les requêtes identiques (plusieurs checks peuvent partager la même requête)
- **Génère `checks_config.yaml`** (source de vérité persistante)

### Étape 2 : Exécution des requêtes

- **dq_runner.py** charge `checks_config.yaml`
- Pour chaque **unique_query** (optimisation : pas d'exécution redondante) :
  - **dremio_client.py** envoie `POST /api/v3/sql`
  - Polling `GET /api/v3/job/{jobId}` (intervalle 2s, timeout 120s)
  - Récupère résultats : `GET /api/v3/job/{jobId}/results`
  - Retourne `{total_lignes, valides, score_completude_pct}`

### Étape 3 : Calcul des scores & flags

Calculs **bottom-up** :

- **Colonne** : `score = (valides / total_lignes) * 100`
- **Table** : `score = avg(colonnes_scores)` ; `total_lignes = max(colonnes_total_lignes)`
- **Global** : `score = avg(tables_scores)`

### Étape 4 : Génération YAML et CSV par run

**yaml_writer.py** + **csv_writer.py** créent un dossier horodaté :

```
output/
└── 2026-06-10_10-26-20/
    ├── run.log
    ├── 2026-06-10_10-26-20_yaml/
    │   ├── _all_tables.yaml
    │   ├── professional_description.yaml
    │   └── ... (une table par fichier)
    └── 2026-06-10_10-26-20_csv/
        ├── _all_tables.csv
        ├── professional_description.csv
        └── ... (une table par fichier)
```

Chaque fichier CSV et YAML contient une colonne **`timestamp`** au format ISO-8601 (ex : `2026-06-10T10:26:20`).

---

## Consolidation pour le reporting (Tableau)

Le script `lib/consolidate.py` agrège les résultats de **tous les runs d'une journée** dans un fichier unique, prêt à être importé dans Tableau ou tout autre outil BI.

### Pourquoi un script séparé ?

Les données Dremio sont ingérées **une fois par nuit**. Donc :
- Les scores ne changent pas dans la journée.
- Mais une requête peut échouer (`ERROR`) si Dremio est surchargé au moment du run.
- Si on relance `lib/run_dq.py` plus tard, les colonnes en erreur obtiennent leurs scores.
- `lib/consolidate.py` fuscionne intelligemment les runs pour avoir un fichier complet sans aucune erreur.

### Règles de consolidation

| Règle | Comportement |
|-------|---|
| Flag `ERROR` | Jamais inclus dans le consolidé |
| Doublon `(dremio_col, virt_full_path, date)` | Ignoré (premier résultat non-ERROR gardé) |
| Nouveaux résultats | Ajoutés en append |

### Workflow quotidien type

```
Matin  → python lib/run_dq.py       → 54 OK, 10 ERROR
         python lib/consolidate.py  → 54 lignes consolidées

Midi   → python lib/run_dq.py       → 10 erreurs du matin passent OK
         python lib/consolidate.py  → 10 nouvelles lignes ajoutées
                                  (les 54 déjà là sont ignorées)

Résultat : 64 lignes complètes dans le consolidé, 0 ERROR
```

### Fichiers produits

```
output/
└── consolidated/
    ├── all_columns_history.csv    ← Prêt pour Tableau
    └── all_columns_history.yaml   ← Pour usage programmatique
```

**Colonnes** :

```
dremio_col, virt_full_path, dataset, domain, rule, total_lignes, valides, score_pct, flag, timestamp
```

---

## Structure du projet

```
quikdirtyDQchecks/
├── lib/
│   ├── __init__.py
│   ├── run_dq.py                  # CLI principal (point d'entrée)
│   ├── consolidate.py             # Consolidation pour le reporting
│   ├── excel_parser.py            # Parsing Excel → checks_config.yaml
│   ├── dremio_client.py           # Wrapper API REST Dremio
│   ├── dq_runner.py               # Orchestration + calcul scores
│   ├── yaml_writer.py             # Génération outputs YAML
│   └── csv_writer.py              # Génération outputs CSV
├── checks_config.yaml             # Config générée (non versionné)
├── Stewardship_Workshop_Template_Tiers_Counterparties_fixed.xlsx
├── .env                           # Secrets Dremio (non versionné)
├── .env.example                   # Modèle pour le .env
├── requirements.txt
├── README.md
└── output/
    ├── consolidated/              # Fichiers reporting cumulatifs
    │   ├── all_columns_history.csv
    │   └── all_columns_history.yaml
    └── 2026-06-10_10-26-20/      # Un dossier par run
        ├── run.log
        ├── 2026-06-10_10-26-20_yaml/
        └── 2026-06-10_10-26-20_csv/
```

---

## Format des outputs

### YAML par table (`customer_job.yaml`)

```yaml
table: customer_job
run_timestamp: '2026-06-10T10:26:20'
total_lignes: 4549263
table_score_pct: 53.1
table_flag: FAIL
columns:
  - dremio_col: code_categorie_socio_professionelle
    virt_full_path: VIRTUALISATION.staging-nova-referentieltiers.customer_job.socioprofessional_category
    dataset: CIHOne.CLIENTS.Personnes_Physiques.clients
    domain: PTP-TIE-POR
    rule: Completude
    total_lignes: 4549263
    valides: 2086324
    score_pct: 45.87
    flag: FAIL
    error: null
    timestamp: '2026-06-10T10:26:20'
```

### CSV par table (`customer_job.csv`)

```
dremio_col,virt_full_path,dataset,domain,rule,total_lignes,valides,score_pct,flag,error,timestamp
code_categorie_socio_professionelle,...,4549263,2086324,45.87,FAIL,,2026-06-10T10:26:20
```

### Résumé global CSV (`_all_tables.csv`)

```
table,total_lignes,score_pct,flag,nb_checks,nb_pass,nb_warn,nb_fail,nb_error,timestamp
professional_description,29472,54.30,FAIL,5,2,1,2,0,2026-06-10T10:26:20
customer_job,4549263,53.10,FAIL,5,1,0,4,0,2026-06-10T10:26:20
```

### Fichier consolidé (`all_columns_history.csv`)

```
dremio_col,virt_full_path,dataset,domain,rule,total_lignes,valides,score_pct,flag,timestamp
genre,...,97949,49765,50.81,FAIL,2026-06-03T14:33:06
adresse,...,8158571,8158571,100.00,PASS,2026-06-03T14:33:06
```

---

## Architecture interne

### lib/excel_parser.py
- Parse feuille Excel `Quality_checks_poc`
- Filtre lignes VIRTUALISATION uniquement
- Déduplique SQL par SHA-256 (query_id)
- Génère/charge `checks_config.yaml`
- Fonctions clés : `parse_excel()`, `generate_config()`, `load_config()`

### lib/dremio_client.py
- Classe `DremioClient` pour API REST Dremio
- `POST /api/v3/sql` → lancement requête
- Polling `GET /api/v3/job/{jobId}` (2s interval, 120s timeout)
- `GET /api/v3/job/{jobId}/results` → résultats
- Retourne `{total_lignes, valides, score_completude_pct}`
- Gestion erreurs : ArithmeticException, 401 Unauthorized, timeouts

### lib/dq_runner.py
- Orchestration principale
- Charge config, boucle sur `unique_queries`
- Appelle `DremioClient` pour chaque requête
- Mappe résultats aux checks individuels
- Calcul scores bottom-up : colonne → table → global
- Dataclasses : `ColumnResult`, `TableResult`, `RunResult`

### lib/yaml_writer.py
- Sérialise `RunResult` en YAML
- 1 fichier par table + 1 résumé global `_all_tables.yaml`
- Ajoute `timestamp` ISO-8601 sur chaque colonne

### lib/csv_writer.py
- Exporte `RunResult` en CSV
- 1 fichier par table + 1 résumé global `_all_tables.csv`
- Colonnes : `dremio_col, virt_full_path, dataset, domain, rule, total_lignes, valides, score_pct, flag, error, timestamp`

---

## Fichier Excel source

**Sheet : `Quality_checks_poc`**

| Col | Nom | Description |
|-----|-----|---|
| A | domain | Domaine métier |
| B | dataset | Dataset source (CIHOne) |
| C | dremio_col | Nom colonne Dremio |
| D | virt_full_path | Chemin VIRTUALISATION complet |
| E | raw_col | Nom colonne brute VIRTUALISATION |
| F | rule | Type check (ex: "Completude") |
| G | sql | Requête SQL |

---

## Troubleshooting

| Erreur | Solution |
|--------|----------|
| `Config file not found: checks_config.yaml` | Lancer `python lib/run_dq.py --refresh-config` depuis le bon dossier |
| `can't open file 'lib/run_dq.py'` | Vérifier que vous êtes bien dans le dossier du projet (`cd quikdirtyDQchecks`) |
| `PermissionError` sur Excel | Fermer le fichier dans Excel |
| `ArithmeticException` Dremio | Vérifier que le SQL cible bien VIRTUALISATION |
| `401 Unauthorized` | Vérifier la clé API dans `.env` |
| `Timeout 120s` | Requête SQL trop lourde, relancer hors heure de pointe |
| Flag `ERROR` dans les outputs | Dremio surchargé — relancer `lib/run_dq.py` puis `lib/consolidate.py` |

---

## Workflow : Ajouter un nouveau check

1. Ajouter une ligne dans l'Excel (colonnes A–G)
2. Lancer `python lib/run_dq.py --refresh-config --run`
3. Consulter `output/{timestamp}/_all_tables.yaml`

---

## Prochaines étapes (roadmap)

- [ ] Support règles DQ supplémentaires (unicité, intégrité ref., conformité patterns)
- [ ] Dashboard web
- [ ] Alertes (email, Slack)
- [ ] Support multiples sources de données

---

## Notes de conformité

**KYC et CSP** : Ces colonnes ont un impact direct sur la **conformité réglementaire**. Les scores FAIL/WARN nécessitent investigation et documentation des causes racines (données source vs. mapping).

---

License : Internal — Stewardship Workshop POC  
Version : 1.1 (ajout CSV, timestamp, consolidation reporting)