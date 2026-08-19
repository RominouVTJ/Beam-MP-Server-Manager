# Déployer Beam-MP-Server-Manager VMware Edition

Ce guide décrit le parcours prévu pour **Beam-MP-Server-Manager v0.11.0 VMware Edition**. Tant que les notes v0.11 indiquent `draft`, la validation runtime finale n’est pas encore clôturée.

L’appliance contient déjà Debian Linux, BeamMP Server et le Web Manager. L’utilisateur normal n’a pas à installer Linux, Python, Git ou Docker.

## 1. Télécharger la release

Les GitHub Releases sont la source officielle.

Si l’OVA est distribuée en plusieurs parties 7-Zip, télécharger **toutes** les parties dans le même dossier, par exemple :

```text
Beam-MP-Server-Manager-v0.11.0.7z.001
Beam-MP-Server-Manager-v0.11.0.7z.002
...
Beam-MP-Server-Manager-v0.11.0-SHA256SUMS.txt
```

Le nombre réel de parties dépend de la taille finale de l’OVA.

Vérifier les SHA-256 indiqués dans `SHA256SUMS`, puis ouvrir le fichier `.7z.001` avec 7-Zip et extraire :

```text
Beam-MP-Server-Manager.ova
```

Ne pas utiliser un fichier dont le hash ne correspond pas.

## 2. Importer dans VMware Workstation

1. Ouvrir VMware Workstation Pro.
2. **File → Open**.
3. Sélectionner `Beam-MP-Server-Manager.ova`.
4. Choisir le nom et le dossier de stockage de la nouvelle VM.
5. Laisser VMware créer une nouvelle identité/MAC pour cette instance.
6. Vérifier que l’adaptateur réseau est connecté.
7. Pour un serveur domestique, le mode **Bridged / Pont** est généralement le plus simple.
8. Démarrer la VM.

Une réservation DHCP est recommandée une fois l’installation terminée afin que l’adresse LAN reste stable.

## 3. First Run local

Au premier démarrage, l’assistant graphique VMware demande :

1. langue ;
2. pays / région ;
3. disposition et test du clavier ;
4. localisation / fuseau horaire ;
5. **compte Linux de maintenance** avec mot de passe ;
6. finalisation.

Le compte Linux est réservé à la maintenance exceptionnelle. Il est distinct des comptes du Web Manager.

Après **Terminer et redémarrer**, l’appliance redémarre automatiquement puis affiche le desktop graphique Beam-MP-Server-Manager. Le compte graphique technique `beamconsole` est automatique et n’est pas un compte de maintenance/SSH.

La fenêtre locale affiche notamment :

- état du Manager ;
- état/configuration BeamMP ;
- URL Web ;
- code de sécurité de l’appliance, masqué par défaut.

Conserver le code de sécurité. Ne jamais le publier dans une capture ou une issue GitHub.

## 4. Créer le premier administrateur Web

Depuis un ordinateur du même réseau :

```text
http://IP_DE_L_APPLIANCE:8765
```

Puis :

1. utiliser le parcours de première connexion ;
2. saisir le code demandé ;
3. créer le premier administrateur Web ;
4. se connecter.

Le Manager Web et le compte Linux de maintenance sont deux identités séparées.

## 5. Configurer BeamMP

Dans le Manager :

1. renseigner l’AuthKey BeamMP ;
2. définir le nom du serveur ;
3. régler le nombre maximal de joueurs/voitures ;
4. choisir public/privé ;
5. choisir la carte ;
6. enregistrer ;
7. démarrer BeamMP.

L’AuthKey est un secret. Ne pas l’envoyer dans un ticket GitHub, une capture ou un log public.

## 6. Ports réseau

BeamMP :

```text
TCP 30814
UDP 30814
```

Web Manager :

```text
TCP 8765
```

Pour autoriser des joueurs depuis Internet, rediriger TCP + UDP `30814` vers l’adresse LAN de l’appliance.

**Ne pas rediriger 8765 vers Internet dans une installation normale.** Le Manager est prévu pour être administré depuis le LAN ou derrière une solution d’accès sécurisée mise en place volontairement.

## 7. Cartes, véhicules et mods

Le Manager permet notamment :

- sélection des cartes officielles ;
- import de cartes moddées ;
- import de véhicules et autres ZIP ;
- activation/désactivation de la distribution client ;
- protection de la carte moddée actuellement sélectionnée ;
- miniatures de cartes officielles lorsque les aperçus BeamNG locaux sont disponibles.

Les fichiers distribués aux clients sont gérés par l’appliance. Il n’est pas nécessaire de déplacer manuellement les ZIP dans Linux pour l’usage normal.

## 8. Live Server

Lorsqu’un client BeamNG/BeamMP rejoint le serveur, la page Live peut afficher :

- joueurs ;
- véhicules ;
- ping ;
- vitesse lorsque disponible ;
- position/radar de déplacement ;
- événements de connexion, véhicule et déconnexion.

Les contrôles prévus incluent message serveur, kick et suppression de véhicule.

Le radar actuel est volontairement un affichage de mouvement local. Il ne prétend pas être une projection calibrée de la Big Map BeamNG.

## 9. Sauvegardes

Les sauvegardes et données runtime sont stockées en dehors du code applicatif. Une mise à jour du Manager ne doit pas supprimer :

- comptes Web ;
- configuration ;
- AuthKey BeamMP ;
- mods/cartes/véhicules ;
- sauvegardes ;
- données runtime utiles.

## 10. Mise à jour du Manager à partir de v0.11

La page **Settings** expose la mise à jour du Manager.

Elle affiche :

- version installée ;
- version disponible ;
- dernier résultat de mise à jour.

Lorsqu’une GitHub Release plus récente contient le package exact attendu et son digest SHA-256, le Manager peut le télécharger et l’installer.

Le mécanisme :

1. télécharge le package officiel ;
2. vérifie SHA-256 ;
3. valide le contenu ;
4. prépare la nouvelle installation ;
5. sauvegarde l’installation et l’état Manager nécessaires au rollback ;
6. redémarre le Manager ;
7. contrôle sa santé et sa version ;
8. restaure automatiquement l’ancienne version si le nouveau Manager ne revient pas sain.

Un package local `.update.zip` peut également être utilisé comme solution de maintenance.

### Important pour v0.10.0

L’OVA v0.10.0 a été créée avant ce mécanisme. Elle ne peut donc pas déclencher elle-même un update Web vers v0.11.0. Le mécanisme intégré commence avec v0.11.0 pour les versions suivantes.

## 11. Signaler un bug / proposer une fonction

Le bouton **Bug / suggestion** ouvre un formulaire puis prépare une issue GitHub.

Les informations techniques automatiques sont volontairement limitées à des informations non sensibles telles que la version du Manager et certains états de santé.

Avant de publier une issue, vérifier qu’elle ne contient jamais :

- AuthKey BeamMP ;
- mot de passe ;
- code de sécurité de l’appliance ;
- cookie/session ;
- clé privée ;
- adresse réseau sensible non anonymisée.

## 12. Maintenance Linux exceptionnelle

L’usage normal ne nécessite pas SSH. Le compte Linux créé pendant First Run sert uniquement lorsqu’une opération de maintenance spécifique le demande.

`beamconsole` n’est pas utilisable en SSH.

Le root direct est verrouillé. Les opérations privilégiées du Manager passent par des helpers limités plutôt que par un accès root générique.

## 13. Factory reset / préparation d’image

La procédure de factory reset est destinée aux opérations de maintenance/image et efface volontairement les comptes/données d’instance concernés. Ne pas l’utiliser comme une simple fonction de redémarrage.

Le helper v0.11 détache le travail destructif de la session utilisateur avant de supprimer les comptes de maintenance, afin que le processus puisse terminer proprement et éteindre la VM.

## 14. Problèmes fréquents

### Pas d’adresse LAN

Vérifier l’adaptateur VMware, le mode Bridged et le DHCP du routeur.

### Manager inaccessible

Depuis le même LAN :

```text
http://IP_DE_L_APPLIANCE:8765
```

Utiliser HTTP sauf si vous avez volontairement ajouté votre propre couche HTTPS/reverse proxy.

### Serveur accessible en LAN mais pas sur Internet

Vérifier :

- réservation DHCP ;
- redirection TCP 30814 ;
- redirection UDP 30814 ;
- IP publique réelle ;
- éventuel CGNAT opérateur.

### Mod non téléchargé

Vérifier son état de distribution dans le Manager, redémarrer BeamMP si nécessaire, puis reconnecter le client.

## 15. Windows Edition

Une Windows Edition native est prévue ultérieurement. Elle utilisera le même core et la même interface Web, mais n’utilisera ni cette VM Debian ni systemd. Elle n’est pas incluse dans v0.11.
