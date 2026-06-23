# 🎯 TABLEAUDQ — Guide Complet : Dashboard Data Quality dans Tableau avec Dremio

> Pipeline de Data Quality (DQ) basé sur **Python → Dremio → Tableau**.  
> Ce repo contient le moteur de checks DQ (`quikdirtyDQchecks`), les données consolidées et le dashboard HTML de visualisation.

---

## 📁 Structure du projet

```
TABLEAUDQ/
├── quikdirtyDQchecks/          # Pipeline Python DQ
│   ├── lib/
│   │   ├── dremio_client.py    # Client REST Dremio (auth, SQL, publish CSV)
│   │   ├── consolidate.py      # Consolidation des runs → CSV/YAML
│   │   ├── dq_runner.py        # Moteur d'exécution des checks
│   │   ├── csv_writer.py       # Écriture des résultats CSV
│   │   └── yaml_writer.py      # Écriture des résultats YAML
│   ├── output/
│   │   ├── consolidated/
│   │   │   └── all_columns_history.csv   # ← Dataset principal (575 lignes)
│   │   └── YYYY-MM-DD_HH-MM-SS/          # Runs horodatés
│   ├── checks_config.yaml      # Configuration des règles DQ
│   ├── .env.example            # Template de configuration
│   └── requirements.txt
├── all_columns_history.csv     # Copie racine des données consolidées
├── kyc_dq_v2.html              # Dashboard HTML interactif
└── README.md                   # Ce fichier
```

---

## 🏗️ Architecture du pipeline

```
Python (quikdirtyDQchecks)
   ↓  exécute checks_config.yaml contre Dremio
   ↓  génère CSV/YAML horodatés par run
output/consolidated/all_columns_history.csv
   ↓  auto-publié via publish_home_csv()
Dremio Home : @S141/TiersDataQualityReport/all_columns_history
   ↓  typed view créée automatiquement (types corrects : BIGINT, DOUBLE, TIMESTAMP)
@S141/TiersDataQualityReport/all_columns_history  ← dataset prêt pour BI
   ↓
Tableau ← connexion ici via les 3 views SQL
```

> **Bonne nouvelle** : Le système Python **publie déjà automatiquement** dans Dremio via `DREMIO_PUBLISH_AUTO=true` dans le `.env`.

---

## 📊 Structure des données (`all_columns_history`)

| Colonne | Type | Description |
|---|---|---|
| `dremio_col` | VARCHAR | Nom de la colonne analysée |
| `virt_full_path` | VARCHAR | Chemin virtuel dans Dremio |
| `dataset` | VARCHAR | Chemin logique (ex: `CIHOne.CLIENTS.Personnes_Physiques.clients`) |
| `domain` | VARCHAR | Domaine métier (ex: `PTP-TIE-POR`) |
| `rule` | VARCHAR | Règle DQ appliquée (ex: `Complétude`) |
| `total_lignes` | BIGINT | Nombre total de lignes |
| `valides` | BIGINT | Lignes valides |
| `score_pct` | DOUBLE | Score en % |
| `flag` | VARCHAR | Résultat : `PASS` / `WARN` / `FAIL` |
| `timestamp` | TIMESTAMP | Date du run |

---

## ✅ Dois-tu créer des SQL queries avant Tableau ?

**→ 3 cas possibles :**

| Scénario | Action |
|---|---|
| **Dashboard simple** | ❌ Pas de SQL — connecte directement sur `all_columns_history` |
| **Vues pré-agrégées (recommandé)** | ✅ Crée les 3 views SQL dans Dremio (voir ci-dessous) |
| **Tout dans Tableau** | ⚠️ Possible mais calculs LOD plus lourds |

**Recommandation : crée les views Dremio** → plus rapide pour Tableau, logique centralisée.

---

## ÉTAPE 1 — Vérifier que ton dataset est dans Dremio

Dans l'UI Dremio, navigue vers :
```
My Home (@S141) > TiersDataQualityReport > all_columns_history
```

Si le dataset n'est pas visible, relance la consolidation :
```bash
cd quikdirtyDQchecks
python lib/consolidate.py --all
```

---

## ÉTAPE 2 — Créer les 3 Views SQL dans Dremio

> 📍 **Chemin Dremio cible :**  
> `INTERNS > Data_Management_Interns > TiersDataQualityReport`  
> URL : `http://dlakegtwprd:9047/space/INTERNAL/INTERNS.Data_Management_Interns.TiersDataQualityReport.TiersDataQualityReport`

Copie-colle ces queries **une par une** dans l'éditeur SQL de Dremio (`http://dlakegtwprd:9047`).  
Les views seront créées dans le **même dossier** que la table source `TiersDataQualityReport`.

### 🔵 View 1 — Score Global par Dataset

```sql
CREATE OR REPLACE VIEW "INTERNS"."Data_Management_Interns"."TiersDataQualityReport"."DQ_vue_score_global" AS
SELECT
    dataset,
    domain,
    rule,
    CAST("timestamp" AS DATE)                                   AS date_run,
    COUNT(*)                                                    AS nb_colonnes_testees,
    ROUND(AVG(score_pct), 2)                                    AS score_moyen_pct,
    SUM(CASE WHEN flag = 'PASS' THEN 1 ELSE 0 END)             AS nb_pass,
    SUM(CASE WHEN flag = 'WARN' THEN 1 ELSE 0 END)             AS nb_warn,
    SUM(CASE WHEN flag = 'FAIL' THEN 1 ELSE 0 END)             AS nb_fail,
    ROUND(
        SUM(CASE WHEN flag = 'PASS' THEN 1 ELSE 0 END) * 100.0 / COUNT(*),
        1
    )                                                           AS taux_pass_pct
FROM "INTERNS"."Data_Management_Interns"."TiersDataQualityReport"."TiersDataQualityReport"
GROUP BY dataset, domain, rule, CAST("timestamp" AS DATE)
ORDER BY date_run DESC, score_moyen_pct ASC;
```

### 🟠 View 2 — Détail Colonnes Enrichi (pour drill-down)

```sql
CREATE OR REPLACE VIEW "INTERNS"."Data_Management_Interns"."TiersDataQualityReport"."DQ_vue_detail_colonnes" AS
SELECT
    dremio_col,
    dataset,
    SPLIT_PART(dataset, '.', 1)                           AS systeme,
    SPLIT_PART(dataset, '.', 2)                           AS domaine_client,
    SPLIT_PART(dataset, '.', 3)                           AS type_personne,
    SPLIT_PART(dataset, '.', 4)                           AS table_source,
    domain,
    rule,
    total_lignes,
    valides,
    (total_lignes - valides)                              AS invalides,
    score_pct,
    flag,
    "timestamp",
    CAST("timestamp" AS DATE)                             AS date_run,
    CASE
        WHEN score_pct >= 90 THEN 'PASS (>=90%)'
        WHEN score_pct >= 70 THEN 'WARN (70-89%)'
        ELSE 'FAIL (<70%)'
    END                                                   AS statut_label
FROM "INTERNS"."Data_Management_Interns"."TiersDataQualityReport"."TiersDataQualityReport";
```

### 🟢 View 3 — Évolution Temporelle (pour trend chart)

```sql
CREATE OR REPLACE VIEW "INTERNS"."Data_Management_Interns"."TiersDataQualityReport"."DQ_vue_evolution" AS
SELECT
    CAST("timestamp" AS DATE)                              AS date_run,
    domain,
    SPLIT_PART(dataset, '.', 3)                            AS type_personne,
    COUNT(*)                                               AS nb_checks,
    ROUND(AVG(score_pct), 2)                               AS score_moyen,
    SUM(CASE WHEN flag = 'FAIL' THEN 1 ELSE 0 END)        AS nb_fail,
    SUM(CASE WHEN flag = 'WARN' THEN 1 ELSE 0 END)        AS nb_warn,
    SUM(CASE WHEN flag = 'PASS' THEN 1 ELSE 0 END)        AS nb_pass
FROM "INTERNS"."Data_Management_Interns"."TiersDataQualityReport"."TiersDataQualityReport"
GROUP BY CAST("timestamp" AS DATE), domain, SPLIT_PART(dataset, '.', 3)
ORDER BY date_run;
```

---

## ÉTAPE 3 — Connecter Tableau à Dremio

### Dans Tableau Desktop :

1. **Se connecter** → Clic sur **"À un serveur"** dans la page d'accueil
2. **Choisir** → **"Dremio"** dans la liste des connecteurs
3. **Remplir les champs** :
   - Serveur : `dlakegtwprd`
   - Port : `9047`
   - Authentification : **Personal Access Token**
   - Token : valeur de `DREMIO_API_KEY` dans ton `.env`
4. **Cliquer** → "Se connecter"

### Sélectionner les datasets :

Dans le panneau gauche de Tableau :
```
Espace       → INTERNS
Dossier      → Data_Management_Interns > TiersDataQualityReport
Tables       → Glisser les 3 views :
  ✅ DQ_vue_score_global
  ✅ DQ_vue_detail_colonnes
  ✅ DQ_vue_evolution
```

---

## ÉTAPE 4 — Structure du Dashboard "Vue Globale" (5 feuilles)

### Feuille 1 — KPI Score Moyen Global
| Paramètre | Valeur |
|---|---|
| Source | `DQ_vue_score_global` |
| Mesure | `score_moyen_pct` (Moyenne) |
| Visuel | Texte grand format (BAN) |
| Filtre | `date_run` = dernière date |

### Feuille 2 — Donut PASS / WARN / FAIL
| Paramètre | Valeur |
|---|---|
| Source | `DQ_vue_score_global` |
| Dimension | `flag` |
| Mesure | `nb_colonnes_testees` (SUM) |
| Visuel | Graphique Secteurs → Anneau |

### Feuille 3 — Barres Top FAIL par Dataset
| Paramètre | Valeur |
|---|---|
| Source | `DQ_vue_score_global` |
| Lignes | `dataset` (trié par score_moyen_pct ASC) |
| Colonnes | `score_moyen_pct` |
| Couleur | `flag` (rouge/orange/vert) |

### Feuille 4 — Évolution Temporelle
| Paramètre | Valeur |
|---|---|
| Source | `DQ_vue_evolution` |
| Colonnes | `date_run` |
| Lignes | `score_moyen` |
| Couleur | `type_personne` |
| Visuel | Graphique Linéaire |

### Feuille 5 — Heatmap Colonnes × Dataset
| Paramètre | Valeur |
|---|---|
| Source | `DQ_vue_detail_colonnes` |
| Lignes | `dremio_col` |
| Colonnes | `type_personne` |
| Couleur | `score_pct` (palette divergente rouge→vert) |

---

## ✅ Checklist de démarrage

```
[ ] 1. Vérifier que all_columns_history existe dans Dremio (@S141)
[ ] 2. Créer les 3 views SQL dans l'éditeur Dremio
[ ] 3. Ouvrir Tableau → Connexion → Dremio
[ ] 4. Saisir host=dlakegtwprd:9047 + ton API key
[ ] 5. Glisser DQ_vue_score_global dans la zone de données
[ ] 6. Créer Feuille 1 (KPI Score Global)
[ ] 7. Créer Feuille 2 (Donut PASS/WARN/FAIL)
[ ] 8. Créer Feuille 3 (Top FAIL par Dataset)
[ ] 9. Créer Feuilles 4 et 5
[ ] 10. Assembler le Dashboard → glisser les 5 feuilles
[ ] 11. Ajouter filtres globaux (date_run, domain)
```

---

## ⚙️ Configuration (.env)

Copie `.env.example` → `.env` et remplis :

```env
# Connexion Dremio
DREMIO_HOST=http://dlakegtwprd:9047
DREMIO_AUTH_TYPE=bearer
DREMIO_API_KEY=<ton_personal_access_token>

# Seuils de scoring
SCORE_PASS_THRESHOLD=90
SCORE_WARN_THRESHOLD=70

# Publication automatique dans Dremio
DREMIO_PUBLISH_AUTO=true
DREMIO_PUBLISH_HOME_NAME=@S141
DREMIO_PUBLISH_FOLDER_PATH=TiersDataQualityReport
DREMIO_PUBLISH_DATASET_NAME=all_columns_history
```

---

## 🚀 Lancer un run DQ

```bash
cd quikdirtyDQchecks

# Installer les dépendances
pip install -r requirements.txt

# Lancer les checks et publier dans Dremio
python lib/run_dq.py

# Consolider tous les runs historiques
python lib/consolidate.py --all
```

---

## 🔒 Sécurité

Le fichier `.env` (contenant l'API key Dremio) est **exclu du repo** via `.gitignore`.  
Ne jamais commiter de credentials en clair.

---

*Projet Data Quality — Domaine PTP-TIE-POR | CIHOne*
