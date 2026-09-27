# n8n-automation

> Documentation complète du projet (architecture, nœuds, installation, évolutions) : `../README.md`.
> Exports des workflows publiés : `workflows/`.
n8n (outil d'automatisation par workflows) en local via Docker, accessible depuis ce PC uniquement ; données dans le volume `n8n_data`.
Démarrer : `docker compose up -d` · Arrêter : `docker compose down` · Logs : `docker compose logs -f`
Interface : http://localhost:5678
Au premier accès, n8n demande de créer un compte propriétaire (email + mot de passe) : il est stocké localement dans le volume, jamais envoyé à un serveur n8n externe en self-hosted.
