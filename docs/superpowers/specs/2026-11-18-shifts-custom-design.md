# Shifts custom + grille avec tous les membres — design

## Pourquoi
Certains events demandent des postes en plus du bar (ex. staff à l'entrée). Le bureau
veut que le solveur puisse les remplir, ou les remplir lui-même. En construisant le
planning, il veut aussi voir les membres qui ont répondu « pas dispo » pour ajouter des heures à quelqu'un qui
s'est rendu dispo après coup.

## Décisions
- Shift custom = poste parallèle au bar, **sur les créneaux existants** (pas d'horaires libres).
- Dispos partagées : dispo 22h = bar OU shift custom 22h, jamais les deux.
- Bar prioritaire en cas de manque de monde ; bureau souhaité sur chaque shift custom.
- Grille : tous les répondants non exclus visibles, y compris ceux qui ont répondu
  « pas dispo » partout (non-répondants via « Ajouter un membre ») ; affectation
  manuelle possible même sur dispo « no » / sans réponse, signalée (W8 + cellule pointillée).

## Approches considérées
1. **Poste sur l'affectation (retenue)** : table `shifts_custom` (une ligne par
   créneau × nom) + `assignments.shift_custom_id` (NULL = bar). Dispos, formulaire,
   CSV, stats `heure_relative` inchangés ; `uq_assignment` garantit 1 place/heure.
2. Nouveaux `Slot` typés : casse `heure_relative`, dispos par poste, formulaire, stats.
3. Dispos par poste : formulaire plus long, rejeté par le bureau.

## Solveur
`y[i][k]` par membre × shift custom ; `x + Σy <= 1` par heure ; pénalités par membre
(pauses, quota, triple, équité, consécutifs, maybe) sur `w = x + Σy`. Custom :
`<= max_personnes` dur, place vide 3000 (< 10000 bar), sans bureau 500.

## Hors scope
Horaires libres, dispo par poste, respo sur poste custom, tools MCP.
