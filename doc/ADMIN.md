## Technical notes

- Non-secret runtime settings: YunoHost settings in `/etc/yunohost/apps/__ID__/settings.yml`
- IMAP passwords: encrypted systemd credentials in `__DATA_DIR__/remote_password.cred` and `__DATA_DIR__/local_imap_password.cred`
- Persistent state cursor: `__DATA_DIR__/state.json`
- Service logs: `journalctl -u __ID__` and `/var/log/__ID__/__ID__.log`
- Main runtime settings are exposed through the app's YunoHost config panel.

The daemon polls the remote IMAP inbox every 60 seconds and appends messages through local IMAP on `127.0.0.1:143`.
