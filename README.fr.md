<p align="center"><img src="docs/logo.png" width="128" alt="Pulse Monitor"></p>

<h1 align="center">Pulse Monitor</h1>

<p align="center"><a href="README.md">English</a> · <b>Français</b></p>

Tableau de bord simple et léger pour Windows : performances du PC en temps réel (façon Gestionnaire des tâches), fiche des composants utile pour changer des pièces, analyse du stockage, santé des disques et rapports de sessions de jeu. 100 % local, aucune télémétrie.

Version actuelle : **v0.8.32**

## Télécharger

Dans les [Releases](https://github.com/leclodo/Pulse-Monitor/releases/latest) :
- **`Pulse-Monitor-vX-Setup.exe`** : installeur (menu Démarrer, raccourci Bureau, désinstallation depuis les Paramètres Windows). Les données vont dans `%LOCALAPPDATA%\Pulse Monitor`.
- **`Pulse-Monitor-vX-portable.zip`** : sans installation, dézippe et lance `Pulse Monitor.exe`. Les données restent dans le dossier.

## Aperçu

![Performances](docs/performances.png)

| Composants | Stockage |
|---|---|
| ![Composants](docs/composants.png) | ![Stockage](docs/stockage.png) |
| **Analyse** | **Gaming** |
| ![Analyse](docs/analyse.png) | ![Gaming](docs/gaming.png) |
| **Programmes les plus gourmands à un moment donné** | **Paramètres** |
| ![Programmes](docs/programmes.png) | ![Paramètres](docs/parametres.png) |

## Ce qu'il affiche

**Performances** (mis à jour chaque seconde)
- Processeur : utilisation, fréquence réelle, charge par cœur
- Mémoire : utilisée / disponible, fichier d'échange
- Cartes graphiques : utilisation, température, mémoire vidéo
- Disques : activité, lecture / écriture, espace libre par lecteur
- Réseau : réception / envoi

**Composants**
- Scan lancé quand tu le décides (bouton « Scanner les composants », puis « Actualiser ») ; le résultat est gardé entre deux lancements
- Processeur (nom de code, cœurs P / E, caches, instructions, virtualisation), carte mère + BIOS (chipset, UEFI, Secure Boot), barrettes de RAM (emplacements, canaux, vitesse, latence, référence, alerte si le profil XMP / EXPO n'est pas activé), cartes graphiques (puce, type de mémoire, PCIe, âge du pilote), disques (HDD/SSD, SATA/NVMe, santé), Windows, réseau, écrans
- Aucun pilote installé : tout est lu avec les outils de Windows
- **Infos à compléter** : alimentation, boîtier, refroidissement, ports M.2/SATA (listes pré-remplies)
- Bouton « Copier le résumé de ma config » pour demander conseil sur un forum

**Stockage** (façon TreeSize)
- Dossiers triés par taille, répartition par type de fichier, plus gros fichiers, « Ouvrir dans l'Explorateur »
- **Santé des disques** : état, température, durée de vie restante (SSD), heures, erreurs (lecture avec les droits administrateur, à la demande)
- **Fichiers temporaires** : fichiers temporaires Windows, mises à jour, caches des navigateurs / applis / shaders, miniatures, rapports de plantage, corbeille. Rien n'est supprimé.

**Analyse**
- Bilan de la semaine (moment le plus chargé et pendant quel jeu, pics de CPU / GPU / VRAM / RAM)
- Programmes au démarrage : ce qui se lance avec Windows, la RAM utilisée par chacun, et un interrupteur pour le désactiver (comme le Gestionnaire des tâches)
- Alertes utiles dans la cloche : disque presque plein, santé d'un disque en baisse, RAM souvent saturée, pilote graphique ancien
- Historique **1 h / 6 h / 12 h / 24 h / 7 jours** (gardé 8 jours) : CPU, GPU + température, VRAM, RAM
- Survol = valeurs, clic = les programmes les plus gourmands (CPU, RAM, GPU) à ce moment-là
- Glisser un graphique (ou ◀ ▶) pour remonter dans le temps ; changer de durée zoome autour du milieu
- Périodes éteintes réduites à une bande étroite (au choix) et toujours indiquées
- Quel composant te limite le plus (après 10 min d'usage intensif)

**Gaming**
- **Mode gaming** : ferme les programmes inutiles pendant que tu joues (synchro cloud, launchers, messageries, navigateurs…) et indique la RAM, le CPU, le disque et le GPU libérés. L'impact de chaque programme est calculé selon la puissance du PC. Scan, verdict clair (le boost n'est proposé que s'il sert vraiment), liste à vérifier (impact de chaque programme), optimisation ; mode automatique en option quand un jeu est détecté. Le système, les pilotes, l'audio, les anti-triche et les antivirus ne sont jamais touchés. « Restaurer » relance tout ensuite
- Temps de jeu par jour, par semaine et par jeu ; signal ⚠ de température dans le widget quand la carte graphique chauffe en jeu
- Jeu reconnu en 10 à 30 s (Steam, Epic, GOG, Battle.net, Riot… ou plein écran avec la carte graphique chargée)
- Rapport de chaque session : durée, CPU / GPU (moyenne / max), température du GPU, VRAM / RAM, **FPS (moyenne / 1 % low)**, ce qui limitait, et graphiques (survol = valeurs, clic = les programmes les plus gourmands)
- FPS lus dans les événements d'affichage de Windows, sans programme ajouté (autorisation unique si besoin : groupe « Utilisateurs du journal de performances »)
- Recherche des jeux installés, ajout manuel de n'importe quel programme, pause / reprise du suivi

## Utilisation

- **Cloche 🔔** : conseils en direct (composant saturé, programme en cause, programmes au démarrage…). ✕ pour masquer un conseil.
- **Clic sur le titre d'une carte** (Performances) : liste des programmes qui utilisent la ressource. **Ctrl + clic** : masquer la carte (à réafficher dans Paramètres).
- Boutons **Cartes : Repliées / Petites / Grandes** en haut de Performances ; poignée **⋮⋮** pour réorganiser par glisser-déposer.
- **Paramètres** : langue FR / EN (celle de Windows par défaut), 5 thèmes, lancement avec Windows, garder en fond à la fermeture, vérification quotidienne des mises à jour.
- **Fermer la fenêtre (✕)** : l'appli reste près de l'horloge avec une petite icône (~30 Mo). Clic = rouvrir, clic droit = Quitter.
- **Widget de bureau** : CPU, GPU + température, VRAM, RAM sur une ligne (compact ou barres), plus ● et les FPS en jeu. 📌 = épinglé au-dessus de tout avec clics à travers (maintenir Ctrl pour le déplacer), 🧲 = aimanté aux bords de l'écran. Opacité et taille réglables, raccourci clavier configurable.
- **Mises à jour** : clic sur le numéro de version → « Vérifier les mises à jour ».
- Survole une donnée pour une explication courte.

## Vie privée

Tout reste sur ton PC : pas de compte, pas de publicité, pas de télémétrie, rien n'est vendu ni partagé. La seule connexion Internet est la vérification des mises à jour auprès de GitHub, quand tu la demandes (ou une fois par jour si tu actives l'option, désactivée par défaut). Détails dans l'appli : icône 🛡 à côté de la version.

## Léger

La fenêtre se ferme complètement quand on la quitte, les mesures ralentissent quand elle est réduite ou sur un autre onglet que Performances, et WebView2 tourne en mode allégé (un seul processus d'affichage, sans accélération GPU).

## Depuis le code source

- **Lancer** : double-clic sur **`Lancer Pulse Monitor.bat`** (installe `psutil`, `pywebview`, `pystray` et `Pillow` au premier lancement), ou `python pulse_monitor.py`.
- **Créer l'installeur et la version portable** : double-clic sur **`Creer l'exe.bat`**. Il installe PyInstaller (et Inno Setup si besoin) et crée les deux fichiers dans le dossier `release`.

## Limites

- Température du processeur : non disponible sans pilote spécial (comme dans le Gestionnaire des tâches).
- Alimentation, boîtier, ventirad et ports M.2/SATA : pas détectables par logiciel → à saisir dans « Infos à compléter ».
- Si la fenêtre s'affiche mal (page blanche) : lancer avec `--full-gpu`.
- En cas de souci, voir `pulse_monitor.log` (dans le dossier des données : Paramètres → Vie privée → « Ouvrir le dossier des données »).
