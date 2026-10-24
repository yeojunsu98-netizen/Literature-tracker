#!/usr/bin/env python3
"""Convert secondary literature copies into Google Drive native shortcuts.

This script reads each topic's live native Google Sheet (CSV fallback) with:
- Storage role: Canonical or Shortcut
- Canonical folder
- Canonical file ID
- Canonical SUP file ID(s)

The semantic classification and choice of the canonical folder are handled by
the literature organizer. This script only enforces the storage layout:
one physical PDF in the canonical folder, shortcuts in secondary topic folders.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import sys
from dataclasses import dataclass
from typing import Any

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

ROOT_FOLDER_ID = os.environ.get(
    "LITERATURE_BOX_FOLDER_ID",
    "1FAbZsNjt0q47KGq3NX2vunckXc3L1CjK",
)
SERVICE_ACCOUNT_JSON = os.environ.get("GDRIVE_SERVICE_ACCOUNT_JSON", "").strip()
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"

SHORTCUT_MIME = "application/vnd.google-apps.shortcut"
PDF_MIME = "application/pdf"
CSV_MIME = "text/csv"

ROLE_COL = "Storage role"
CANONICAL_FOLDER_COL = "Canonical folder"
CANONICAL_ID_COL = "Canonical file ID"
CANONICAL_SUP_IDS_COL = "Canonical SUP file ID(s)"
FILENAME_COL = "Main PDF filename"
SUP_FILENAMES_COL = "SUP filename(s)"
DOI_COL = "DOI"
TITLE_COL = "Title"


@dataclass
class DriveItem:
    id: str
    name: str
    mime_type: str
    shortcut_target_id: str = ""


def load_credentials():
    if not SERVICE_ACCOUNT_JSON:
        raise RuntimeError(
            "Missing GDRIVE_SERVICE_ACCOUNT_JSON GitHub secret. "
            "Add the service-account JSON as a repository Actions secret."
        )
    try:
        info = json.loads(SERVICE_ACCOUNT_JSON)
    except json.JSONDecodeError as e:
        raise RuntimeError("GDRIVE_SERVICE_ACCOUNT_JSON is not valid JSON") from e

    return service_account.Credentials.from_service_account_info(
        info,
        scopes=["https://www.googleapis.com/auth/drive"],
    )


def drive_service():
    return build("drive", "v3", credentials=load_credentials(), cache_discovery=False)


def list_all(service, q: str, fields: str = "id,name,mimeType,shortcutDetails,parents") -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    token = None
    while True:
        resp = (
            service.files()
            .list(
                q=q,
                spaces="drive",
                fields=f"nextPageToken,files({fields})",
                pageToken=token,
                pageSize=1000,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )
        out.extend(resp.get("files", []))
        token = resp.get("nextPageToken")
        if not token:
            return out


def list_child_folders(service, parent_id: str) -> list[dict[str, Any]]:
    q = (
        f"'{parent_id}' in parents and trashed=false and "
        "mimeType='application/vnd.google-apps.folder'"
    )
    return list_all(service, q)


def list_folder_items(service, folder_id: str) -> list[DriveItem]:
    rows = list_all(service, f"'{folder_id}' in parents and trashed=false")
    out = []
    for r in rows:
        target = ((r.get("shortcutDetails") or {}).get("targetId") or "")
        out.append(
            DriveItem(
                id=r["id"],
                name=r.get("name", ""),
                mime_type=r.get("mimeType", ""),
                shortcut_target_id=target,
            )
        )
    return out


def download_text_file(service, file_id: str) -> str:
    request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buf.getvalue().decode("utf-8-sig")


def find_topic_csv(service, folder_id: str, folder_name: str) -> dict[str, Any] | None:
    files = list_all(
        service,
        f"'{folder_id}' in parents and trashed=false and mimeType='{CSV_MIME}'",
        fields="id,name,mimeType,parents",
    )
    if not files:
        return None

    preferred = folder_name.replace("/", " ") + ".csv"
    exact = [f for f in files if f.get("name") == preferred]
    if exact:
        return exact[0]

    # Fall back to the first topic CSV. Exclude common non-index CSV names.
    candidates = [
        f for f in files
        if not f.get("name", "").lower().startswith("shortcut ")
        and "manifest" not in f.get("name", "").lower()
    ]
    return candidates[0] if candidates else None


def find_topic_sheet(service, folder_id: str, folder_name: str) -> dict[str, Any] | None:
    """Prefer the live native Google Sheet to a potentially stale CSV mirror."""
    files = list_all(
        service,
        f"'{folder_id}' in parents and trashed=false and "
        "mimeType='application/vnd.google-apps.spreadsheet'",
        fields="id,name,mimeType,parents",
    )
    preferred = folder_name.replace("/", " ")
    matches = [f for f in files if f.get("name") == preferred]
    return matches[0] if matches else (files[0] if len(files) == 1 else None)


def export_sheet_csv(service, sheet_id: str) -> str:
    request = service.files().export_media(fileId=sheet_id, mimeType="text/csv")
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buf.getvalue().decode("utf-8-sig")


def sync_topic_csv(service, folder_id: str, folder_name: str,
                   csv_file: dict[str, Any] | None, sheet_text: str) -> None:
    """Update the existing CSV in place from the authoritative native Sheet."""
    if csv_file:
        old_text = download_text_file(service, csv_file["id"])
        if old_text.replace("\\r\\n", "\\n").strip() == sheet_text.replace("\\r\\n", "\\n").strip():
            return
    name = folder_name.replace("/", " ") + ".csv"
    print(f"  sync CSV mirror: {name}")
    if DRY_RUN:
        return
    media = MediaIoBaseUpload(io.BytesIO(sheet_text.encode("utf-8")),
                             mimetype=CSV_MIME, resumable=False)
    if csv_file:
        service.files().update(
            fileId=csv_file["id"], media_body=media,
            fields="id,name", supportsAllDrives=True,
        ).execute()
    else:
        service.files().create(
            body={"name": name, "mimeType": CSV_MIME, "parents": [folder_id]},
            media_body=media, fields="id,name", supportsAllDrives=True,
        ).execute()


def parse_csv(text: str) -> list[dict[str, str]]:
    reader = csv.DictReader(io.StringIO(text))
    return [{k: (v or "").strip() for k, v in row.items()} for row in reader]


def split_semicolon(value: str) -> list[str]:
    return [x.strip() for x in value.split(";") if x.strip()]


def clean_file_id(value: str) -> str:
    value = value.strip()
    m = re.search(r"/d/([A-Za-z0-9_-]+)", value)
    if m:
        return m.group(1)
    return value


def get_file(service, file_id: str) -> dict[str, Any] | None:
    try:
        return (
            service.files()
            .get(
                fileId=file_id,
                fields="id,name,mimeType,parents,trashed,shortcutDetails,md5Checksum",
                supportsAllDrives=True,
            )
            .execute()
        )
    except Exception as e:
        print(f"WARNING: cannot access canonical file {file_id}: {e}")
        return None


def create_shortcut(service, *, target_id: str, folder_id: str, name: str) -> str | None:
    print(f"  create shortcut: {name} -> {target_id}")
    if DRY_RUN:
        return "DRY_RUN"
    body = {
        "name": name,
        "mimeType": SHORTCUT_MIME,
        "shortcutDetails": {"targetId": target_id},
        "parents": [folder_id],
    }
    created = (
        service.files()
        .create(
            body=body,
            fields="id,name,mimeType,shortcutDetails",
            supportsAllDrives=True,
        )
        .execute()
    )
    return created.get("id")


def delete_file(service, file_id: str, name: str):
    print(f"  delete duplicate physical file: {name} ({file_id})")
    if DRY_RUN:
        return
    service.files().delete(fileId=file_id, supportsAllDrives=True).execute()


def ensure_shortcut(
    service,
    *,
    folder_id: str,
    folder_items: list[DriveItem],
    target_id: str,
    display_name: str,
):
    if not target_id or not display_name:
        return

    # First check whether the correct native shortcut already exists.
    has_shortcut = False
    for item in folder_items:
        if item.mime_type == SHORTCUT_MIME and item.shortcut_target_id == target_id:
            has_shortcut = True
            if item.name != display_name:
                print(f"  existing shortcut target OK; rename {item.name} -> {display_name}")
                if not DRY_RUN:
                    service.files().update(
                        fileId=item.id,
                        body={"name": display_name},
                        fields="id,name",
                        supportsAllDrives=True,
                    ).execute()
            break

    # Create before deleting any secondary physical copy.
    if not has_shortcut:
        shortcut_id = create_shortcut(
            service,
            target_id=target_id,
            folder_id=folder_id,
            name=display_name,
        )
        if not shortcut_id:
            return
        # Keep in-memory inventory current to avoid creating duplicate shortcuts.
        folder_items.append(
            DriveItem(id=shortcut_id, name=display_name,
                      mime_type=SHORTCUT_MIME, shortcut_target_id=target_id)
        )

    # Remove only byte-identical secondary PDFs. Never infer identity solely
    # from matching filenames or file sizes: versions can legitimately differ.
    canonical = get_file(service, target_id)
    canonical_md5 = (canonical or {}).get("md5Checksum")
    if not canonical_md5:
        print(f"  no canonical checksum; preserve secondary PDFs: {display_name}")
        return
    for item in list(folder_items):
        if item.id == target_id or item.name != display_name or item.mime_type != PDF_MIME:
            continue
        candidate = get_file(service, item.id)
        if candidate and candidate.get("md5Checksum") == canonical_md5:
            delete_file(service, item.id, item.name)
            if not DRY_RUN:
                folder_items.remove(item)
        else:
            print(f"  preserve potentially different PDF version: {item.name} ({item.id})")


def main() -> int:
    service = drive_service()

    topic_folders = list_child_folders(service, ROOT_FOLDER_ID)
    if not topic_folders:
        print("No topic folders found.")
        return 0

    print(f"Found {len(topic_folders)} topic folders.")

    for folder in topic_folders:
        folder_id = folder["id"]
        folder_name = folder.get("name", folder_id)

        # The Sheet is authoritative; CSV mirrors can lag when connector egress
        # restrictions prevent in-place CSV replacement.
        sheet_file = find_topic_sheet(service, folder_id, folder_name)
        csv_file = find_topic_csv(service, folder_id, folder_name)
        if not sheet_file and not csv_file:
            print(f"[{folder_name}] no topic index; skip")
            continue

        try:
            if sheet_file:
                sheet_text = export_sheet_csv(service, sheet_file["id"])
                rows = parse_csv(sheet_text)
                print(f"[{folder_name}] reading live Google Sheet")
                try:
                    sync_topic_csv(service, folder_id, folder_name, csv_file, sheet_text)
                except Exception as sync_error:
                    print(f"[{folder_name}] CSV sync failed (continuing): {sync_error}")
            else:
                rows = parse_csv(download_text_file(service, csv_file["id"]))
                print(f"[{folder_name}] reading fallback CSV")
        except Exception as e:
            print(f"[{folder_name}] cannot read index: {e}")
            continue

        if not rows:
            print(f"[{folder_name}] empty CSV")
            continue

        if ROLE_COL not in rows[0] or CANONICAL_ID_COL not in rows[0]:
            print(
                f"[{folder_name}] CSV has no '{ROLE_COL}' / '{CANONICAL_ID_COL}' columns; "
                "waiting for organizer to update schema."
            )
            continue

        shortcut_rows = [
            r for r in rows if r.get(ROLE_COL, "").strip().lower() == "shortcut"
        ]
        if not shortcut_rows:
            print(f"[{folder_name}] no shortcut rows")
            continue

        print(f"[{folder_name}] {len(shortcut_rows)} shortcut rows")
        folder_items = list_folder_items(service, folder_id)

        for row in shortcut_rows:
            canonical_id = clean_file_id(row.get(CANONICAL_ID_COL, ""))
            filename = row.get(FILENAME_COL, "")
            identity = row.get(DOI_COL) or row.get(TITLE_COL) or filename
            if not canonical_id or not filename:
                print(f"  skip incomplete row: {identity}")
                continue

            canonical = get_file(service, canonical_id)
            if not canonical or canonical.get("trashed"):
                print(f"  canonical missing/trashed: {identity}")
                continue

            ensure_shortcut(
                service,
                folder_id=folder_id,
                folder_items=folder_items,
                target_id=canonical_id,
                display_name=filename,
            )

            # Supporting information follows the same one-physical-file rule.
            sup_names = split_semicolon(row.get(SUP_FILENAMES_COL, ""))
            sup_ids = [clean_file_id(x) for x in split_semicolon(row.get(CANONICAL_SUP_IDS_COL, ""))]
            if len(sup_names) != len(sup_ids):
                if sup_names or sup_ids:
                    print(
                        f"  SUP metadata mismatch for {identity}: "
                        f"{len(sup_names)} filename(s), {len(sup_ids)} canonical ID(s)"
                    )
                continue

            for sup_name, sup_id in zip(sup_names, sup_ids):
                sup = get_file(service, sup_id)
                if not sup or sup.get("trashed"):
                    print(f"  canonical SUP missing: {sup_name}")
                    continue
                ensure_shortcut(
                    service,
                    folder_id=folder_id,
                    folder_items=folder_items,
                    target_id=sup_id,
                    display_name=sup_name,
                )

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
