#!/usr/bin/env python3
"""Forward messages from a remote IMAP inbox into a local YunoHost mailbox."""

from __future__ import annotations

import argparse
import imaplib
import json
import logging
import os
import re
import signal
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
from email.utils import formataddr, getaddresses
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

import yaml

LOGGER = logging.getLogger("email_forward")
INTERNALDATE_PATTERN = re.compile(rb'INTERNALDATE "([^"]+)"')
UIDVALIDITY_PATTERN = re.compile(rb'UIDVALIDITY (\d+)')
RECIPIENT_HEADERS = ("To", "Cc", "Bcc")
LOCAL_IMAP_HOST = "127.0.0.1"
LOCAL_IMAP_PORT = 143


@dataclass(slots=True)
class Config:
    remote_email: str
    remote_password: str
    remote_host: str
    remote_port: int
    delete_remote: bool
    target_user: str
    poll_interval_seconds: int
    target_mailbox: str
    local_imap_email: str
    local_imap_password: str

    @classmethod
    def load(cls, *, app: str) -> "Config":
        data = load_yunohost_settings(app)
        return cls(
            remote_email=str(data["remote_email"]),
            remote_password=load_systemd_credential("remote_password"),
            remote_host=normalize_host(str(data["remote_host"])),
            remote_port=int(data["remote_port"]),
            delete_remote=parse_bool(data["delete_remote"]),
            target_user=str(data["target_user"]),
            poll_interval_seconds=max(1, min(60, int(data.get("poll_interval_minutes", 1)))) * 60,
            target_mailbox=str(data.get("target_mailbox", "INBOX")),
            local_imap_email=str(data["local_imap_email"]),
            local_imap_password=load_systemd_credential("local_imap_password"),
        )


@dataclass(slots=True)
class State:
    last_transfer_at: datetime
    last_uid: int | None
    uidvalidity: int | None

    @classmethod
    def load(cls, path: Path) -> "State":
        if not path.exists():
            return cls(last_transfer_at=_utc_now(), last_uid=None, uidvalidity=None)

        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            last_transfer_at=_parse_datetime(data["last_transfer_at"]),
            last_uid=data.get("last_uid"),
            uidvalidity=data.get("uidvalidity"),
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "last_transfer_at": self.last_transfer_at.isoformat(),
            "last_uid": self.last_uid,
            "uidvalidity": self.uidvalidity,
        }
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            temp_path = Path(handle.name)
        temp_path.replace(path)


class EmailForwarder:
    def __init__(self, config: Config, state_path: Path) -> None:
        self.config = config
        self.state_path = state_path
        self.stop_requested = False

    def run(self) -> None:
        LOGGER.info(
            "Starting IMAP forwarder for remote mailbox %s -> local user %s",
            self.config.remote_email,
            self.config.target_user,
        )
        while not self.stop_requested:
            try:
                processed = self.sync_once()
                if processed:
                    LOGGER.info("Transferred %s message(s) during this cycle", processed)
            except Exception:
                LOGGER.exception("Transfer cycle failed")
            self._sleep(self.config.poll_interval_seconds)
        LOGGER.info("Stop requested, leaving cleanly")

    def sync_once(self) -> int:
        state = State.load(self.state_path)
        processed = 0
        mailbox = None
        try:
            mailbox = self._connect_remote()
            current_uidvalidity = self._fetch_uidvalidity(mailbox)
            uids = self._search_candidate_uids(mailbox, state, current_uidvalidity)
            LOGGER.debug("Found %s candidate message(s)", len(uids))
            for uid in uids:
                message_date, raw_message = self._fetch_message(mailbox, uid)
                if not self._is_newer_than_cursor(message_date, uid, state):
                    continue

                sanitized_message = strip_remote_recipient(raw_message, self.config.remote_email)
                deliver_locally_via_imap_append(
                    sanitized_message,
                    mailbox_name=self.config.target_mailbox,
                    received_at=message_date,
                    login_email=self.config.local_imap_email,
                    login_password=self.config.local_imap_password,
                )

                if self.config.delete_remote:
                    self._delete_remote_message(mailbox, uid)

                state.last_transfer_at = message_date
                state.last_uid = uid
                state.uidvalidity = current_uidvalidity
                state.save(self.state_path)
                processed += 1
        finally:
            if mailbox is not None:
                try:
                    mailbox.logout()
                except Exception:
                    LOGGER.debug("IMAP logout failed", exc_info=True)
        return processed

    def request_stop(self, *_args: object) -> None:
        LOGGER.info("Signal received, stopping after current cycle")
        self.stop_requested = True

    def _sleep(self, seconds: int) -> None:
        deadline = time.monotonic() + seconds
        while not self.stop_requested:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(1.0, remaining))

    def _connect_remote(self) -> imaplib.IMAP4:
        timeout = 30
        if self.config.remote_port == 993:
            mailbox: imaplib.IMAP4 = imaplib.IMAP4_SSL(
                self.config.remote_host,
                self.config.remote_port,
                timeout=timeout,
            )
        else:
            mailbox = imaplib.IMAP4(self.config.remote_host, self.config.remote_port, timeout=timeout)
            try:
                mailbox.starttls()
            except imaplib.IMAP4.error:
                LOGGER.warning("Remote IMAP server does not support STARTTLS on port %s", self.config.remote_port)
        mailbox.login(self.config.remote_email, self.config.remote_password)
        status, _data = mailbox.select("INBOX")
        ensure_ok(status, "Unable to select remote INBOX")
        return mailbox

    def _fetch_uidvalidity(self, mailbox: imaplib.IMAP4) -> int | None:
        status, data = mailbox.status("INBOX", "(UIDVALIDITY)")
        ensure_ok(status, "Unable to read UIDVALIDITY")
        if not data or not data[0]:
            return None
        match = UIDVALIDITY_PATTERN.search(data[0])
        return int(match.group(1)) if match else None

    def _search_candidate_uids(
        self,
        mailbox: imaplib.IMAP4,
        state: State,
        current_uidvalidity: int | None,
    ) -> list[int]:
        if state.uidvalidity is not None and state.last_uid is not None and state.uidvalidity == current_uidvalidity:
            status, data = mailbox.uid("SEARCH", None, "UID", f"{state.last_uid + 1}:*")
            ensure_ok(status, "Unable to search messages by UID")
        else:
            search_date = (state.last_transfer_at - timedelta(days=1)).strftime("%d-%b-%Y")
            status, data = mailbox.uid("SEARCH", None, "SINCE", search_date)
            ensure_ok(status, "Unable to search messages by date")
        return parse_uid_list(data)

    def _fetch_message(self, mailbox: imaplib.IMAP4, uid: int) -> tuple[datetime, bytes]:
        status, data = mailbox.uid("FETCH", str(uid), "(UID INTERNALDATE RFC822)")
        ensure_ok(status, f"Unable to fetch message UID {uid}")

        raw_message = None
        internaldate = None
        for item in data:
            if not isinstance(item, tuple):
                continue
            metadata, payload = item
            raw_message = payload
            match = INTERNALDATE_PATTERN.search(metadata)
            if match:
                internaldate = datetime.strptime(
                    match.group(1).decode("ascii"),
                    "%d-%b-%Y %H:%M:%S %z",
                ).astimezone(timezone.utc).replace(microsecond=0)

        if raw_message is None or internaldate is None:
            raise RuntimeError(f"Incomplete FETCH response for UID {uid}")
        return internaldate, raw_message

    def _delete_remote_message(self, mailbox: imaplib.IMAP4, uid: int) -> None:
        status, _data = mailbox.uid("STORE", str(uid), "+FLAGS.SILENT", r"(\\Deleted)")
        ensure_ok(status, f"Unable to mark remote UID {uid} as deleted")
        if b"UIDPLUS" in mailbox.capabilities:
            status, _data = mailbox.uid("EXPUNGE", str(uid))
            ensure_ok(status, f"Unable to expunge remote UID {uid}")
            return

        LOGGER.warning(
            "Remote server lacks UIDPLUS support; expunge may remove other already-deleted messages"
        )
        status, _data = mailbox.expunge()
        ensure_ok(status, f"Unable to expunge remote UID {uid}")

    @staticmethod
    def _is_newer_than_cursor(message_date: datetime, uid: int, state: State) -> bool:
        if message_date > state.last_transfer_at:
            return True
        if message_date < state.last_transfer_at:
            return False
        if state.last_uid is None:
            return False
        return uid > state.last_uid


def strip_remote_recipient(raw_message: bytes, remote_email: str) -> bytes:
    message = BytesParser(policy=policy.SMTP).parsebytes(raw_message)
    target = remote_email.casefold()

    for header_name in RECIPIENT_HEADERS:
        addresses = getaddresses(message.get_all(header_name, []))
        if not addresses:
            continue
        kept = [item for item in addresses if item[1].casefold() != target]
        del message[header_name]
        if kept:
            message[header_name] = ", ".join(formataddr(item) for item in kept)

    message["X-Forwarded-By"] = "email_forward YunoHost"
    message["X-Forwarded-Source"] = remote_email
    return message.as_bytes(policy=policy.SMTP)


def deliver_locally_via_imap_append(
    raw_message: bytes,
    *,
    mailbox_name: str,
    received_at: datetime,
    login_email: str,
    login_password: str,
) -> None:
    mailbox = imaplib.IMAP4(LOCAL_IMAP_HOST, LOCAL_IMAP_PORT, timeout=30)
    try:
        mailbox.login(login_email, login_password)
        status, _data = mailbox.append(mailbox_name, None, received_at, raw_message)
        ensure_ok(status, f"Local IMAP APPEND failed for mailbox {login_email}")
    finally:
        try:
            mailbox.logout()
        except Exception:
            LOGGER.debug("Local IMAP logout failed", exc_info=True)


def parse_uid_list(data: Iterable[bytes | None]) -> list[int]:
    if not data:
        return []
    payload = b" ".join(item for item in data if item)
    if not payload.strip():
        return []
    return sorted(int(chunk) for chunk in payload.split())


def ensure_ok(status: str, message: str) -> None:
    if status != "OK":
        raise RuntimeError(message)


def normalize_host(value: str) -> str:
    candidate = value.strip()
    if "://" not in candidate:
        return candidate

    parsed = urlparse(candidate)
    if parsed.hostname:
        return parsed.hostname
    raise ValueError(f"Invalid IMAP host or URL: {value}")


def parse_bool(value: object) -> bool:
    return str(value).strip().lower() not in {"", "0", "false", "no", "non"}


def load_yunohost_settings(app: str) -> dict[str, object]:
    settings_path = Path("/etc/yunohost/apps") / app / "settings.yml"
    with settings_path.open(encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle) or {}
    if not isinstance(loaded, dict):
        raise RuntimeError(f"Unexpected settings format in {settings_path}")
    return loaded


def load_systemd_credential(name: str) -> str:
    credentials_directory = os.environ.get("CREDENTIALS_DIRECTORY")
    if not credentials_directory:
        raise RuntimeError("CREDENTIALS_DIRECTORY is not defined")

    credential_path = Path(credentials_directory) / name
    return credential_path.read_text(encoding="utf-8").rstrip("\n")


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(timezone.utc).replace(microsecond=0)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", required=True)
    parser.add_argument("--state", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    args = parse_args(argv)
    config = Config.load(app=args.app)
    forwarder = EmailForwarder(config, args.state)

    signal.signal(signal.SIGINT, forwarder.request_stop)
    signal.signal(signal.SIGTERM, forwarder.request_stop)

    forwarder.run()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        raise SystemExit(0)
