# Pulse Monitor

Version actuelle : **v0.5.4** (numéro dans `APP_VERSION` en haut de `pulse_monitor.py`).

Tableau de bord simple et léger des performances du PC (façon Gestionnaire des tâches), fiche des composants et conseils en direct pour réduire les charges.

## Lancer

Double-clic sur **`Lancer Pulse Monitor.bat`**. Au premier lancement, il installe `psutil`, `pywebview`, `pystray` et `Pillow` (une fois pour toutes).

Sans le .bat : `python pulse_monitor.py`

## Ce qu'il affiche

**Performances** (mis à jour chaque seconde, 60 s d'historique)
- Processeur : utilisation, fréquence réelle, charge par cœur
- Mémoire : utilisée / disponible, fichier d'échange
- Cartes graphiques : utilisation, température, mémoire vidéo
- Disques : activité, lecture / écriture, espace libre par lecteur
- Réseau : réception / envoi

**Composants**
- Processeur, carte mère + BIOS, barrettes de RAM (emplacements, vitesse, référence), cartes graphiques, disques (HDD/SSD, SATA/NVMe), Windows, réseau, écrans
- Bouton « Copier le résumé de ma config » pour demander conseil sur un forum

## Utilisation

- Onglet **Paramètres** : langue FR / EN, thèmes (Sombre, Clair, Noir OLED, Bleu nuit, Terminal) et ordre des catégories.
- Poignée **⋮⋮** (au survol d'un titre) : glisser-déposer pour réorganiser cartes et catégories.
- Clique sur le titre d'une carte ou d'une section pour la replier / déplier. Langue et catégories repliées sont retenues (`settings.json`).
- « Cette appli » (en haut) montre le CPU et la RAM utilisés par Pulse Monitor ; survole pour le détail par processus.
- Onglet Composants → **Infos à compléter** : alimentation, boîtier, refroidissement, ports M.2/SATA (listes pré-remplies). Ajouté au résumé copié.
- **Cloche 🔔** (en haut) : conseils en direct (CPU/RAM/VRAM/température/disque saturés, programme en cause, programmes au démarrage…). Point rouge qui pulse = quelque chose ne va pas. ✕ pour masquer un conseil (celui du démarrage revient si le nombre de programmes change). 
- Boutons **Cartes : Repliées / Petites / Grandes** en haut de Performances.
- Onglet **Stockage** (façon TreeSize) : analyse un disque, dossiers triés par taille (clic pour descendre), répartition par type de fichier, plus gros fichiers, bouton « Ouvrir dans l'Explorateur ». Résultats gardés dans `scans/`.
- Onglet **Analyse** : **historique 24 h / 7 jours** (CPU, GPU + température, RAM, VRAM ; survol = valeurs + programme en cause ; 1 point/min dans `history/`, gardé 8 jours) et quel composant te limite.
- **Santé des disques** : dans l'onglet Stockage (bandeau du disque choisi) et dans ≡ Détails d'un disque → état, température, durée de vie restante (SSD), heures, erreurs. Lecture via les droits administrateur (bouton, à la demande ; résultat gardé).
- **Sessions de jeu** (onglet Gaming) : un jeu est reconnu en 10 à 30 s (dossier Steam/Epic/Battle.net… ou plein écran avec le GPU chargé). À la fin : résumé dans la cloche + ligne dans le tableau (durée, GPU/CPU moyenne/max, température, RAM/VRAM max, ce qui limitait). Gardé dans `games.json` (40 dernières). Désactivable.
- **Fichiers temporaires** (onglet Stockage, sous les disques) : taille des fichiers temporaires Windows, mises à jour, caches des navigateurs/applis/shaders, miniatures, rapports de plantage, corbeille. Rien n'est supprimé : bouton pour ouvrir chaque dossier et le nettoyage de Windows.
- Widget : raccourci clavier configurable (Paramètres) pour l'afficher / le masquer.
- Bouton **≡ Détails** sur les cartes Processeur, Mémoire, Carte graphique et Disque : liste des programmes qui utilisent la ressource (calculé seulement quand le panneau est ouvert).
- Survole une donnée pour une explication courte.
- **Ctrl + clic** sur une carte de Performances pour la masquer ; à réafficher dans Paramètres → Cartes masquées.
- Carte repliée : même largeur, moins haute, avec les infos principales et un mini-graphique.
- **Fermer la fenêtre (✕)** : l'appli reste près de l'horloge avec une petite icône (~30 Mo, la fenêtre est entièrement libérée). Clic = rouvrir, clic droit = Quitter. Désactivable dans Paramètres.
- **Paramètres → Démarrage et fermeture** : lancer Pulse Monitor avec Windows (en fond, sans ouvrir la fenêtre).
- **Widget de bureau** (Paramètres ou clic droit sur l'icône) : petite fenêtre semi-transparente avec CPU, GPU + température, VRAM, RAM. Sur une ligne, style compact (textes) ou barres. 📌 = épinglé au-dessus de tout avec clics à travers (Ctrl maintenu pour le déplacer / désépingler). Nécessite tkinter (inclus avec Python pour Windows).
- **Mises à jour** : clic sur le numéro de version → « Vérifier les mises à jour ». Seule connexion Internet de l'appli (GitHub), uniquement sur demande. Installer garde tes données et copie l'ancienne version dans `backup/`.
- **Vie privée** : icône 🛡 à côté de la version (section en bas des Paramètres).
- Relancer le .bat alors que l'appli tourne déjà rouvre simplement la fenêtre.
- Fenêtre réduite → l'appli se met en veille, arrête presque toutes les mesures et rend sa RAM inutilisée à Windows.
- La fenêtre (WebView2) tourne en mode allégé : sans accélération GPU, un seul processus d'affichage.

## Limites

- Température du processeur : non disponible sans pilote spécial (comme dans le Gestionnaire des tâches).
- Alimentation, boîtier, ventirad et ports M.2/SATA : pas détectables par logiciel → à saisir dans « Infos à compléter ».
- Si la fenêtre s'affiche mal (page blanche), lance `python pulse_monitor.py --full-gpu` et préviens-moi.
- En cas de souci, voir `pulse_monitor.log`.
