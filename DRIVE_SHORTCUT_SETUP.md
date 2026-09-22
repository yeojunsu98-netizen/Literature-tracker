# Google Drive shortcut automation

The literature organizer decides which topic is the **canonical** location for each paper.
This GitHub Action then enforces:

- one physical PDF in the highest-relevance topic folder;
- Google Drive native shortcuts in every other matching topic folder;
- the same rule for Supporting Information when canonical SUP IDs are available.

## One-time setup

1. In Google Cloud Console, create a project (or use an existing one).
2. Enable **Google Drive API**.
3. Create a **Service Account**.
4. Create a JSON key for that service account.
5. Copy the service-account email (it ends with `iam.gserviceaccount.com`).
6. In Google Drive, share the **Literature box** folder with that service-account email as **Editor**.
7. In GitHub, open this repository:
   **Settings → Secrets and variables → Actions → New repository secret**
8. Secret name:
   `GDRIVE_SERVICE_ACCOUNT_JSON`
9. Paste the **entire JSON key file contents** as the secret value.
10. Open **Actions → Drive shortcut sync → Run workflow**.

Do not upload or commit the JSON key file to this repository.

## Schedule

The shortcut workflow runs at **20 minutes past every hour**, after the literature organizer's on-the-hour classification pass.

## Required CSV columns

Each topic CSV is expected to contain these extra columns:

- `Storage role` — `Canonical` or `Shortcut`
- `Canonical folder`
- `Canonical file ID`
- `Canonical SUP file ID(s)`

The organizer maintains these values. The GitHub Action does not decide scientific relevance; it only converts secondary physical copies into native Drive shortcuts.
