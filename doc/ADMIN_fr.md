## Notes techniques

- Parametres non secrets : settings YunoHost dans `/etc/yunohost/apps/__ID__/settings.yml`
- Mots de passe IMAP : credentials systemd chiffres dans `__DATA_DIR__/remote_password.cred` et `__DATA_DIR__/local_imap_password.cred`
- Curseur persistant : `__DATA_DIR__/state.json`
- Logs du service : `journalctl -u __ID__` et `/var/log/__ID__/__ID__.log`
- Les parametres d'execution courants sont exposes dans le panneau de configuration YunoHost de l'app.

Le daemon interroge la boite IMAP distante toutes les 60 secondes et ajoute les messages via IMAP local sur `127.0.0.1:143`.
