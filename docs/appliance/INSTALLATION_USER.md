# Installation utilisateur de l’appliance OVA

Version : **0.10.0**

Ce guide concerne l’OVA validée `Beam-MP-Server-Manager.ova`.

## 1. Vérifier le fichier avant import

Conserver ensemble :

- `Beam-MP-Server-Manager.ova`
- `Beam-MP-Server-Manager.ova.sha256.txt`

Comparer le SHA-256 de l’OVA avec le fichier compagnon avant déploiement si le fichier a été copié ou téléchargé.

## 2. Importer dans VMware

1. Ouvrir VMware Workstation.
2. `File` → `Open` et sélectionner `Beam-MP-Server-Manager.ova`.
3. Donner un nom propre à la nouvelle VM.
4. Choisir son dossier de stockage.
5. Laisser VMware générer une nouvelle identité réseau / MAC pour cette nouvelle instance.
6. Démarrer la VM.

Ne pas cloner une installation déjà configurée pour créer un nouveau serveur destiné à un autre utilisateur. L’OVA est le point de départ propre.

## 3. Premier démarrage

Le wizard local demande successivement :

1. langue ;
2. pays / région ;
3. disposition clavier et test ;
4. locale et fuseau horaire ;
5. création du compte Linux de maintenance.

À la fin, l’écran affiche :

- l’adresse IP LAN de l’appliance ;
- l’URL du Manager Web, sur le port `8765` ;
- un code d’appairage Web à usage initial.

Exemple :

```text
http://192.168.1.50:8765
```

## 4. Créer le premier administrateur Web

Depuis un navigateur situé sur le même réseau local :

1. ouvrir l’URL affichée par la VM ;
2. utiliser le code d’appairage affiché à l’écran ;
3. créer le premier compte administrateur Web.

Le compte Web et le compte Linux de maintenance sont indépendants.

## 5. Configurer BeamMP

Dans le Manager Web :

1. configurer une AuthKey BeamMP valide ;
2. choisir une carte ;
3. régler le nom et les paramètres du serveur ;
4. démarrer BeamMP.

Ports principaux :

- Manager Web : TCP `8765` sur le LAN ;
- BeamMP : TCP et UDP `30814`.

Pour un serveur accessible depuis Internet, la box/routeur et le pare-feu doivent permettre le trafic BeamMP nécessaire vers l’adresse LAN de l’appliance. Il n’est pas nécessaire d’exposer le Manager Web `8765` sur Internet pour le fonctionnement normal du serveur.

## 6. Connexion des joueurs

Sur le réseau local, un client BeamNG/BeamMP peut se connecter à :

```text
IP_DE_L_APPLIANCE:30814
```

Le panneau `Serveur Live` affiche les joueurs connectés, leur véhicule et la télémétrie disponible sans rechargement manuel du navigateur.

## 7. Mods et cartes

Les mods envoyés depuis l’interface peuvent être distribués aux clients BeamMP. Une carte moddée sélectionnée comme carte active est protégée contre la désactivation/suppression tant qu’elle reste sélectionnée.

Les cartes BeamNG officielles disposent de leurs miniatures intégrées dans cette OVA validée. Les mods pouvant fournir leur propre miniature conservent leur comportement dédié.

## 8. Sauvegardes

Utiliser la page `Sauvegardes` du Manager pour les opérations prévues par l’interface. Les sauvegardes et données utilisateur ne font pas partie de l’OVA d’origine et restent propres à chaque installation.

## 9. Règle de mise à jour

Ne jamais modifier le fichier OVA de référence après validation. Une nouvelle version du produit doit être produite depuis la source, validée, exportée puis testée en clean-room avant distribution.
