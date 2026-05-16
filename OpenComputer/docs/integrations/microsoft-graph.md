# Microsoft Graph integration

OpenComputer can read your Microsoft 365 mail, calendar, and OneDrive — and
send mail — through the [Microsoft Graph](https://learn.microsoft.com/graph/)
API. This is Milestone 3 of the Hermes + OpenClaw parity plan.

The integration is **opt-in and inert until you sign in**: with no Graph
account connected, the three Graph tools are not registered and OpenComputer
behaves exactly as it does without this feature.

## What it adds

| Tool | Does | Consent tier |
|------|------|--------------|
| `GraphSendMail` | Send an email from the signed-in account | `PER_ACTION` — confirms the recipient + subject before *every* send |
| `GraphListCalendar` | List calendar events in a time window | `EXPLICIT` — granted once at sign-in |
| `GraphListDriveFiles` | List files/folders in OneDrive | `EXPLICIT` — granted once at sign-in |

`GraphSendMail` is gated `PER_ACTION` deliberately: sending email is
irreversible and outward-facing, so each send is confirmed individually rather
than blanket-authorized once.

## Prerequisite — an Azure app registration

Microsoft's device-code sign-in needs a **public-client app registration** in
Azure AD. There is no shipped OpenComputer app — you register your own
(one-time, free):

1. In the [Azure portal](https://portal.azure.com) → **App registrations** →
   **New registration**. Any name; no redirect URI is needed.
2. Under **Authentication**, enable **Allow public client flows**.
3. Under **API permissions**, add the delegated Microsoft Graph permissions
   `Mail.Send`, `Calendars.Read`, `Files.Read`, and `offline_access`.
4. Copy the **Application (client) ID**.

Then export it:

```bash
export OPENCOMPUTER_GRAPH_CLIENT_ID="<application-client-id>"
```

`OPENCOMPUTER_GRAPH_TENANT` is optional — it defaults to `common` (personal
Microsoft accounts *and* work/school accounts). Set it to a specific tenant id
only if you must restrict sign-in.

Until `OPENCOMPUTER_GRAPH_CLIENT_ID` is set, `oc auth login graph` fails with a
clear message and the Graph tools stay absent.

## Sign in

```bash
oc auth login graph
```

This runs the OAuth **device-code** flow: OpenComputer prints a URL and a
one-time code; open the URL in any browser, enter the code, and approve the
requested permissions. The token is stored in the profile's `auth_tokens.json`
(file mode `0600`) under the provider key `graph`.

```bash
oc auth logout graph   # sign out — deletes the stored token
```

The three Graph tools are registered at process start **only when a Graph
token is stored** — they appear after the first `oc auth login graph` (and a
restart) and disappear again after `oc auth logout graph` (and a restart).

### Token refresh

Microsoft access tokens last about an hour. Because the `offline_access` scope
is requested, a refresh token is stored alongside the access token, and
OpenComputer refreshes proactively just before expiry — so you sign in only
once. If the session is later revoked, a Graph tool returns a clear "run
`oc auth login graph` again" message.

## The tools

### `GraphSendMail`

Sends an email as the signed-in account (`POST /me/sendMail`). Parameters:
`to` (a list of recipient addresses — each is validated as a well-formed email
address *before* anything is sent), `subject`, `body`, and optional `cc`,
`bcc`, and `body_type` (`Text` — the default — or `HTML`). A send is **never
retried**: re-sending risks delivering a duplicate email, so any failure is
surfaced as-is rather than replayed.

### `GraphListCalendar`

Lists calendar events in a time window (`GET /me/calendarView`, which expands
recurring events into individual occurrences). Parameters: `start` and `end`
(ISO-8601; a value with no timezone offset is treated as UTC). With no window
given it defaults to the next 7 days.

### `GraphListDriveFiles`

Lists files and folders in OneDrive (`GET /me/drive/root/children`).
Parameter: optional `folder_path` (e.g. `Documents/Reports`); omit it to list
the drive root.

## Scopes

Sign-in requests exactly four delegated scopes: `Mail.Send`, `Calendars.Read`,
`Files.Read`, and `offline_access` (required for the refresh token). Apart from
sending mail, the integration is read-only — it never modifies your calendar
or files.

## Not included (deferred)

Microsoft **Teams** and **SharePoint** are out of scope for this milestone.
Only mail, calendar, and OneDrive are supported.
