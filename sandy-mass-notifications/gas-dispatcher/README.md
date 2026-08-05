# GAS Mail Dispatcher — deploy runbook (member-support@)

One-time deployment, done **while logged in as `member-support@hellolanding.com`**
(PRD D3 — this account is the sender identity and owns the quota).

## Steps (manual paste — simplest for a one-off under a different account)

1. In a browser profile logged in as **member-support@hellolanding.com**, open
   [script.google.com](https://script.google.com) → **New project**.
2. Name it `Mass Notifications Dispatcher`.
3. Replace the default `Code.gs` content with **`Dispatcher.gs`** from this folder.
4. Project Settings (gear) → check **"Show appsscript.json manifest file"** →
   replace its content with the manifest below (also in this folder as
   `appsscript.json`, untracked — the repo .gitignore excludes all
   appsscript.json files). `ANYONE_ANONYMOUS` is required — the caller is a
   Cloudflare worker with no Google identity; auth is the shared secret, not
   Google login.

   ```json
   {
     "timeZone": "America/Mexico_City",
     "dependencies": {},
     "exceptionLogging": "STACKDRIVER",
     "runtimeVersion": "V8",
     "webapp": {
       "executeAs": "USER_DEPLOYING",
       "access": "ANYONE_ANONYMOUS"
     }
   }
   ```
5. Project Settings → **Script Properties** → add:
   - `DISPATCH_SECRET` = a long random string. Generate one locally:
     `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`
6. **Deploy → New deployment → Web app**:
   - Execute as: **Me (member-support@hellolanding.com)**
   - Who has access: **Anyone**
   - Click Deploy, authorize the Gmail/Drive scopes, copy the `/exec` URL.
7. Hand back to the build session:
   - `MN_DISPATCH_URL` = the `/exec` URL
   - `MN_DISPATCH_SECRET` = the secret from step 5
   These go into Sandy as **workflow secrets** on `mass-notify-dispatch`
   (CLI: `workflows secrets-set`) — never committed to the repo.

## Verify

```bash
curl -s -X POST '<EXEC_URL>' -H 'Content-Type: application/json' \
  -d '{"secret":"<SECRET>","mode":"health"}'
# → {"status":"ok","account":"member-support@hellolanding.com","quotaRemaining":<N>,"version":"mn-dispatcher v1.0.0"}
```

`quotaRemaining` is the live Workspace send budget (~1,500/day). The Sandy app
surfaces it before every campaign.

## Notes

- Requests are JSON-POST only; bad secret → `{"status":"error","message":"unauthorized"}`
  (GAS always returns HTTP 200 — callers must check the body).
- `mode: "draft"` creates Gmail drafts in the member-support@ mailbox — used by
  the app's dry-run.
- Attachment behavior preserves legacy parity: single folder ID expands to its
  direct children, Google-native files export as PDF, 20 MB cap, error strings
  `ATTACH_NOT_FOUND id=…` / `ATTACH_TOO_LARGE total=…`.
- Redeploying after edits: Deploy → **Manage deployments** → edit → new version
  (the `/exec` URL is stable across versions of the SAME deployment).
