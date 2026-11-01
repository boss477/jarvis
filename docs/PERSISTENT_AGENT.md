# Persistent communication agent

The communication hub lets one Windows action agent work across WhatsApp,
Telegram, Slack, Discord, Teams, X, Instagram, email, and voice. Provider credentials are optional;
unconfigured providers remain visible but return an explicit configuration
message instead of silently dropping work.

The hub gives the Windows agent one shared place to route messages and actions.
It does not grant provider access by itself: connect each provider and supply
its credentials, account permissions, and webhook configuration.

## Provider configuration

Set these environment variables in `friday/.env` when a provider is enabled:

```ini
WHATSAPP_ACCESS_TOKEN=
WHATSAPP_PHONE_NUMBER_ID=
WHATSAPP_VERIFY_TOKEN=
META_APP_SECRET=
WHATSAPP_ALLOWED_SENDERS=
TELEGRAM_BOT_TOKEN=
SLACK_BOT_TOKEN=
SLACK_SIGNING_SECRET=
DISCORD_BOT_TOKEN=
TELEGRAM_WEBHOOK_SECRET=
TEAMS_WEBHOOK_URL=
X_USER_ACCESS_TOKEN=
LINKEDIN_ACCESS_TOKEN=
LINKEDIN_AUTHOR_URN=
LINKEDIN_API_VERSION=202610
INSTAGRAM_ACCESS_TOKEN=
INSTAGRAM_BUSINESS_ACCOUNT_ID=
SMTP_HOST=
SMTP_PORT=465
SMTP_USERNAME=
SMTP_PASSWORD=
EMAIL_FROM=
EMAIL_SUBJECT=Message from Jarvis
AGENT_WEBHOOK_TOKEN=
TWILIO_ACCOUNT_SID=
TWILIO_AUTH_TOKEN=
TWILIO_FROM_NUMBER=
PUBLIC_VOICE_WEBHOOK_URL=
VOICE_WEBHOOK_HOST=127.0.0.1
VOICE_WEBHOOK_PORT=8768
VOICE_SESSIONS_PATH=
LLM_BASE_URL=http://127.0.0.1:1234/v1
LLM_MODEL=
LLM_API_KEY=local
AGENT_FILE_ROOTS=
```

Public deployment should terminate TLS at a reverse proxy. The listener binds to
loopback by default; expose it only through a configured HTTPS reverse proxy.
The host refuses non-loopback values for `AGENT_WEBHOOK_HOST` and
`VOICE_WEBHOOK_HOST`, so neither listener can be moved onto a LAN/public
interface via environment configuration. Keep the proxy bound to `127.0.0.1`
and publish only its filtered port through the tunnel.
The shared `AGENT_WEBHOOK_TOKEN` protects webhook routes without provider
signatures (including email, Teams, and X). WhatsApp and Instagram always
require a valid Meta signature; requests fail closed when `META_APP_SECRET` is
missing, even if the generic bearer token is valid. Telegram and Slack signature
checks use their provider headers when the corresponding secrets are configured.

### Windows HTTPS ingress (optional)

For a home Windows PC, a Cloudflare Tunnel can publish a hostname to a local
reverse proxy without opening inbound router ports. Configure its public
hostname to forward to `http://127.0.0.1:8769`; the included Windows proxy
binds only to loopback and forwards only `/whatsapp` to the agent on port 8767.
Every other path, including `/healthz`, returns 404. Keep the agent and proxy
listeners bound to `127.0.0.1`. The Caddy example remains available if you also
need to proxy voice callbacks.

#### Temporary development tunnel

For a first test, use a [Cloudflare Quick Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/do-more-with-tunnels/trycloudflare/).
The included Python proxy means Caddy is not required for WhatsApp. From the
repository root, open separate PowerShell windows and run:

```powershell
# Window 1: local persistent agent
.\run_jarvis_agent_host.bat

# Window 2: loopback-only proxy; forwards /whatsapp and blocks /healthz
.\run_jarvis_whatsapp_proxy.bat

# Window 3: temporary public HTTPS endpoint to the local proxy
cloudflared tunnel --url http://127.0.0.1:8769
```

Copy the `https://….trycloudflare.com` URL printed by `cloudflared`; test
`https://<that-host>/whatsapp` as the Meta callback. Quick Tunnel hostnames are
temporary and can change when restarted, so this is for setup/testing, not the
always-on deployment. For persistent use, configure a named Cloudflare Tunnel
hostname and keep the path-filtering proxy in front of the agent. The local
health check remains `http://127.0.0.1:8767/healthz` and must not be published.

In `friday/.env`, configure the WhatsApp Cloud credentials from Provider
configuration, including a non-empty `WHATSAPP_ALLOWED_SENDERS` list. Start the host, proxy,
and the Cloudflare Tunnel. In Meta's webhook settings, set the callback URL to
`https://<your-public-hostname>/whatsapp`, enter the exact configured
`WHATSAPP_VERIFY_TOKEN` as Meta's verification token, and subscribe to the
messages webhook field. The callback URL and verification token are separate
values. Meta's
signature is checked with `META_APP_SECRET`; requests from phone numbers not in
the allowlist are ignored before reaching the planner.

On Windows, use `friday/.env.example` as a reference. If `friday/.env` does
not exist, copy the example to that name; if it already exists, edit it and add
the WhatsApp entries without replacing the file, since it may contain other
Friday settings. Keep the access token and app secret private, choose a separate
unguessable `WHATSAPP_VERIFY_TOKEN`, and add only your own trusted phone number(s)
to `WHATSAPP_ALLOWED_SENDERS`. Run `uv run python -m friday.services.agent_host --check`
from the repository folder to check Meta configuration and a loopback LLM without
starting the host; remote LLM endpoints are deliberately not probed. After the
check passes, start the host with `run_jarvis_agent_host.bat` and check
`http://127.0.0.1:8767/healthz` locally. Install the optional Windows sign-in
supervisor only after a successful manual start and local health check.

The Windows launcher runs the same local readiness check before starting
and pauses with a setup hint when required WhatsApp credentials or the local
model are missing. This check cannot verify that Meta's public HTTPS webhook
has been registered or that Cloudflare Tunnel is routing it correctly. Treat
webhook registration and public ingress as separate inbound prerequisites: the
host can start after the local check passes, but inbound WhatsApp messages will
not arrive until the callback is configured and reachable. Android setup is
not part of this flow.

Meta's setup UI may change, but the required assets are a Meta app with the
WhatsApp product, a WhatsApp Business Account/phone number, the Phone Number ID,
an access token, and the App Secret. In Meta's WhatsApp webhook configuration,
register the public `/whatsapp` URL, enter the exact `WHATSAPP_VERIFY_TOKEN`,
verify/save, and subscribe to `messages`. See Meta's
[WhatsApp Cloud API collection](https://www.postman.com/meta/whatsapp-business-platform/documentation/wlk6lh4/whatsapp-cloud-api)
and [webhook setup collection](https://www.postman.com/meta/whatsapp-business-platform/folder/lboq68h/webhooks).

To verify the local proxy before configuring Meta, request the WhatsApp GET
verification path through the proxy using the configured verify token and a test
challenge. The response should be the exact challenge text. Do not use
`/healthz` as the public tunnel target. For conversational Twilio calls, set
`PUBLIC_VOICE_WEBHOOK_URL` to
`https://<your-public-hostname>/voice/incoming`; the optional Caddy
configuration also forwards the `/voice/respond` and `/voice/status` callbacks.
The included Python proxy is WhatsApp-only.

Cloudflare Tunnel's published-hostname route maps the public host to the local
proxy, which constrains forwarding to the WhatsApp webhook path. For voice
callback forwarding, use the optional Caddy example. See
[Cloudflare Tunnel setup](https://developers.cloudflare.com/tunnel/get-started/)
and [Caddy handle routing](https://caddyserver.com/docs/caddyfile/directives/handle).

WhatsApp inbound requires `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`,
`WHATSAPP_VERIFY_TOKEN`, `META_APP_SECRET`, and `WHATSAPP_ALLOWED_SENDERS`; the generic
`AGENT_WEBHOOK_TOKEN` is not a substitute for Meta's signature header. WhatsApp
subscription verification uses `WHATSAPP_VERIFY_TOKEN`; Slack event signatures
use `SLACK_SIGNING_SECRET` when configured.
Set `WHATSAPP_ALLOWED_SENDERS` to a comma-separated list of trusted phone
numbers in international format (for example, `+15551234567`). The host defaults
to denying all WhatsApp senders when the list is empty or invalid. Rejected
senders are acknowledged to Meta without being passed to the planner or task
command handler.
The WhatsApp webhook agent accepts text messages and interactive button/list
replies as normal conversation turns; interactive replies preserve both the
visible title and provider reply ID. When Meta includes a matching contact
profile, the sender display name and any replied-to message ID are preserved as
context, with the display name explicitly treated as untrusted metadata. Image,
audio, video, document, and sticker
messages receive an explicit notice that their contents are unavailable, rather
than being silently dropped or claimed as understood; image captions are passed
through as text.

For a WhatsApp-originated task waiting on an approval prompt, the webhook sends
interactive **Approve** and **Deny** buttons when the prompt fits the provider's
1024-character body limit. A tap maps to the existing `/approve` or `/deny`
command; task state is rechecked and the action is bound to the original
channel, sender, and conversation. If buttons are unavailable or the prompt is
too long, use the typed command. Button taps still require a valid Meta webhook
signature and the configured sender allowlist.
If the owner replies with a short confirmation such as `yes` while exactly one
task is awaiting approval, the agent re-shows that task's approval controls but
does not approve it or create a second task; review the earlier exact-action
prompt and tap **Approve** or send `/approve <task-id>`. If multiple approvals
are pending, the owner must choose a task ID explicitly.

## Constructing the agent

The planner is deliberately injected. It can be backed by the existing local
LM Studio/OpenAI-compatible model, OpenRouter, or a deterministic planner for
tests. The planner returns durable task steps:

```python
from friday.services.agent_factory import build_persistent_agent

def planner(_prompt):
    # This example uses a fixed response. Do not parse or echo the formatted
    # planner prompt to recover user text; it is not a structured user input.
    return [{"tool": "respond", "args": {"text": "Hello from the example planner."}}]

runtime = build_persistent_agent(planner)
runtime.start()
```

For the configured local/cloud OpenAI-compatible model, use
`services.agent_planner.plan_with_openai_compatible_model` as the planner.
For a local Ollama server, set `LLM_BASE_URL=http://127.0.0.1:11434/v1` and
`LLM_MODEL` to an installed model name from `ollama list` (for example,
`qwen3:8b`). Keep Ollama running on this PC; the host setup check probes only
loopback model endpoints and never sends credentials to a remote endpoint.

Run the host from the repository root with:

```powershell
uv run python -m friday.services.agent_host
```

On Windows, `run_jarvis_agent_host.bat` starts the persistent webhook host in a
console window. Keep it running to receive configured provider webhooks. For
WhatsApp Cloud, configure the provider callback to the host's `/whatsapp` route
through a public HTTPS reverse proxy; do not expose the loopback listener
directly to the internet.
To opt into starting the host at Windows sign-in, run
`powershell -ExecutionPolicy Bypass -File .\setup_jarvis_agent_startup.ps1`.
This creates one minimized shortcut in your user Startup folder; it does not
run until the next sign-in. A single-instance supervisor writes host output to
`%LOCALAPPDATA%\Friday\logs\agent-supervisor.log` and restarts an exited host
with bounded backoff. Pass `-Remove` to request supervisor stop and remove that
exact shortcut. Stopping during an active action may leave that task marked
uncertain; it will not be replayed automatically. The sign-in shortcut starts
only the agent host; it does not start the WhatsApp proxy or Cloudflare Tunnel.
Start those separately before testing inbound WhatsApp webhooks, or configure
them as persistent services if they must survive sign-in or reboot. The host
stays in the background as the always-on process; open the composer separately
when you want to create or review a composed send. The host and
composer share the durable task log, with an operating-system file lock so
task refreshes and approval consumption stay consistent across processes.
The host prints a local `/healthz` URL; opening it reports worker liveness,
consecutive background errors, voice callback configuration/listener status, and
whether an error is present, without returning exception details or conversation
content. It separately reports which WhatsApp Cloud outbound and inbound setup
requirements are missing, plus whether WhatsApp Web outbound fallback is
available; it never returns credential values or allowlisted phone numbers.
Webhook bodies are limited to 1 MiB and oversized requests are rejected
before their body is read. Transient worker failures are retried
with bounded backoff instead of permanently stopping the background worker;
warning logs include only the exception class and failure count.
Webhook batches are handled one message at a time, including reply delivery;
if a later message fails, earlier replies are already recorded and duplicate
events in Meta's retried batch are suppressed. Inbound messages are persisted as
pending before planning and marked processed
only after a reply is computed. Failed planning attempts can therefore be
retried by provider redelivery; completed redeliveries reuse the cached reply
instead of creating a second task. The communication hub leaves polled event
deduplication to this durable processing layer, so failed poll-based events can
also be retried. Transient webhook-processing and delivery failures return HTTP
503 without exception details so webhook providers can retry; the cached reply
is reused once processing has completed. A retry after a confirmed successful
reply delivery is acknowledged without sending the same reply again; an
unconfirmed or failed delivery can retry the cached reply without repeating the
agent action. Concurrent deliveries of the same WhatsApp event reserve the
reply send by provider message ID, so only one request sends it at a time;
overlapping duplicate requests are acknowledged without sending another reply.
As with most provider webhooks, a process crash between the
provider accepting a send and the local delivery record being persisted can
still result in a duplicate send.

To open the local Windows multi-channel composer:

```powershell
uv run python -m friday.services.compose_window
```

Or double-click `run_jarvis_compose.bat` from File Explorer.

Choose a channel from the dropdown, enter its explicit recipient or destination
ID, and add it to the destination list. Add multiple rows to fan out one message,
then use **Prepare for approval** and **Approve & send**. All selected destinations are submitted as
one durable, approval-gated task. The pending-approval picker reloads saved
tasks when the composer opens and can refresh them, so approvals remain
accessible after closing and reopening the window. Available names include `whatsapp`, `x`,
`x_public`, `linkedin_public`, `instagram`, `instagram_public`, `slack`, `discord`, `teams`, `telegram`, `email`, and `voice`; each requires
the appropriate provider-specific recipient identifier and configuration.
Use `x` for a direct message to a numeric X user ID; use `x_public | public` to
create a public X post. Both are approval-gated destinations.
Use `linkedin_public | public` to publish a text-only LinkedIn post. Configure
`LINKEDIN_ACCESS_TOKEN` with the `w_member_social` permission and set
`LINKEDIN_AUTHOR_URN` to the authorized `urn:li:person:...` or
`urn:li:organization:...`. `LINKEDIN_API_VERSION` is optional and defaults to
`202610`; update it as LinkedIn retires API versions. This destination creates
public posts only; LinkedIn direct messaging is not implemented.
Use `instagram` for a direct message to an Instagram-scoped user ID. Use
`instagram_public | https://...` to publish a captioned image: put the caption in
the message field and supply a publicly reachable HTTPS image URL as the
recipient. This requires an Instagram Professional account and the appropriate
Meta content-publishing permission; consumer accounts are not supported.
Meta's API creates a media container and then publishes it, and requires the
media URL to be fetchable by Meta.
The composer has **Open provider settings** and **Refresh setup** controls for
editing `friday/.env` locally and updating the readiness display without
printing credential values. Its readiness summary separates WhatsApp Cloud
outbound from WhatsApp inbound and names the missing inbound credentials.
Restart the persistent host after changing webhook settings; restart the
composer too if changing `VOICE_SESSIONS_PATH`.
Completed compositions retain per-destination outcomes. The composer can prepare
an approval-gated retry containing only failed destinations; it warns that a
provider may have accepted a send before returning an error, so duplicates are
possible.
For WhatsApp outbound composition, the Cloud API is used when its token and
phone-number ID are configured; otherwise the composer uses Friday's persistent
Chrome profile and WhatsApp Web, which must be signed in (scan the QR code when
prompted). The persistent inbound webhook host still requires WhatsApp Cloud
webhook configuration; the browser fallback is outbound only. WhatsApp Web
sends require one exact, visible contact match; ambiguous or missing matches are
refused rather than sending to the first chat in the list.

It starts the agent and webhook listener on port 8767. Set
`AGENT_WEBHOOK_HOST`, `AGENT_WEBHOOK_PORT`, `AGENT_WEBHOOK_TOKEN`, and
`AGENT_TASKS_PATH` to configure the listener and durable state location. The
gateway keeps the existing `/check`, `/pause`, `/resume`, `/cancel`, `/approve`,
and `/deny` commands while routing ordinary messages into the agent. `/help`
lists the command syntax. Incomplete or unknown slash commands return usage
guidance instead of being interpreted as ordinary AI requests. `/pending`
lists approval-waiting and interrupted tasks created by the current channel,
sender, and conversation; it never approves or executes them.
`build_persistent_agent` also exposes its webhook gateway, task manager,
controller, and orchestrator for embedding. Conversation events are appended to
`conversations.jsonl` beside the configured task file. Recent planner context is
limited to the prior 12 non-empty user/agent messages in that same channel and
conversation; internal processed/delivery journal events and the current inbound
message are excluded. The journal itself remains append-only for webhook
deduplication and crash recovery, so it contains message text and should be
treated as private local data.

## Safety model

The agent does not bypass the existing task/orchestrator policy. A task can be
queued, paused, resumed, cancelled, completed, failed, or placed in
`waiting_approval`. Tasks, conversation turns, delivery results, and saved
memory are persisted locally. WhatsApp memory is scoped to the sender ID and
channel; old unscoped memory remains local-owner-only. Requests to recall a fact
use a host-side scoped lookup, and replies are assembled from saved records
rather than guessed by the planner. Outbound messages, browser clicks/forms, and
calendar creation wait for an explicit `/approve <task-id>` command (or the
equivalent owner-bound WhatsApp approval button). Remote
task commands are bound to the task's originating channel, sender, and
conversation; desktop-composer tasks can only be managed in the local composer.
Planner output cannot set task execution state: the controller copies only the
tool name, arguments, and user-facing step text, and initializes every new step
as pending and unapproved before it enters the durable task store. The memory
scope is likewise overwritten from the inbound channel and sender, never
accepted from model-generated arguments.
Approval prompts show the outgoing channel, recipient, and exact message (or
action-specific details); password-like browser-fill values are redacted.
Per-task execution is serialized and approval state transitions are atomic. A
step must be durably claimed while the task is still running immediately before
its tool handler is invoked, so pause/cancel received before that claim prevents
the side effect from starting. Concurrent webhook retries cannot execute the
same task step twice in-process.
The host holds a single-instance lock for its task store. On startup, tasks left
in `running` are marked failed with an interrupted step and an uncertain-result
warning; they are never automatically replayed. Use `/pending` or `/check <id>`
to review them and verify the destination before creating a new retry.
Pausing or cancelling after the durable step claim cannot undo that in-flight
operation; its result is checkpointed, while the paused/cancelled state prevents
subsequent task steps from starting. A completed final in-flight step remains
completed even if a pause/cancel arrived after the external operation began.
Agent file searches are restricted to `AGENT_FILE_ROOTS` (Windows path-list
separator `;`), defaulting to the current user's home directory; opening a path
is always approval-gated.
Ordinary replies to the initiating conversation use the reply-only `respond`
tool and are delivered automatically; the tool cannot choose another recipient.
Messages to other recipients/channels still use approval-gated `send` or
`compose_send` actions. The controller suppresses explicit completion claims
unless a matching approval-gated action step actually succeeded; a call request
being accepted by Twilio is not treated as proof that the recipient answered.
Older persisted tasks that predate ownership metadata remain visible in the
composer for local approval or cancellation, but are not remotely controllable.

## Current limitations

X direct messages require a numeric recipient ID and a user access token with
the required DM scopes. Public posts use the separate `x_post` task and require
approval. Email delivery uses SMTP; inbound email arrives as normalized JSON
at `/email`, so an email service or relay must forward messages there.
Teams currently uses a configured outgoing webhook URL and accepts normalized
inbound JSON; it is not yet a full Microsoft Bot Framework or Graph integration.
Multi-destination sends are not atomic: each provider can accept or reject
independently. The task records a SENT/FAILED result for every destination;
review failed destinations before retrying to avoid duplicate sends.

The default host registers communication, browser, desktop, files, memory,
research, and calendar workers. It exposes web search, browser reading/control,
file search/open, calendar creation, and persistent remember/recall. Additional
provider actions and business connectors need app-specific credentials and
registrations.

- Instagram messaging requires an approved business account and Graph API
  permissions.
- Calling requires a telephony provider adapter and recording/consent policy.
- Twilio voice calls use the approved message as their opener. Configure
  `PUBLIC_VOICE_WEBHOOK_URL` as a public HTTPS URL and reverse-proxy the voice
  webhook port (default 8768) to the local host, forwarding the configured
  incoming path, `/voice/respond`, and `/voice/status`, to enable turn-by-turn
  AI conversation. Calls identify the caller as an AI assistant; Twilio
  transcribes speech and the configured LLM responds with no action
  tools, limited to the approved call purpose. Active call context is stored
  in a short-lived local session file shared between the composer and host,
  with active context expiring after two hours and removed on Twilio's call
  completion callback. On Windows it defaults under `%LOCALAPPDATA%\Friday`;
  set `VOICE_SESSIONS_PATH` to override it. This file contains the call opener
  and recent transcribed turns, so protect it as private data. Keep
  `TWILIO_AUTH_TOKEN` configured so webhook signatures are verified. Without
  the public callback URL, calls remain one-way spoken messages. This is
  turn-based speech, not full-duplex interruption or live audio streaming;
  those require a Twilio Media Streams worker. Twilio's `<Gather>` posts
  recognized speech to its action URL for the next turn
  ([Twilio documentation](https://www.twilio.com/docs/voice/twiml/gather)).
  The voice model can end the call with an explicit goodbye instead of
  repeatedly prompting for another turn.
  A successful task result means Twilio accepted the call request; it does
  not confirm that the recipient answered or that a conversation completed.
  To place calls, configure `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, and
  `TWILIO_FROM_NUMBER`. Use an explicit E.164 recipient number (for example,
  `+15551234567`) and provide a concise approved call opener/purpose. Set
  `PUBLIC_VOICE_WEBHOOK_URL` to the externally
  reachable HTTPS callback URL; the Windows host listens on
  `VOICE_WEBHOOK_HOST`/`VOICE_WEBHOOK_PORT` (defaults `127.0.0.1:8768`). The
  configured `LLM_BASE_URL`, `LLM_MODEL`, and `LLM_API_KEY` power each spoken
  reply. LLM callbacks have an 11-second timeout to stay within Twilio's
  15-second voice webhook ceiling
  ([Twilio webhook settings](https://www.twilio.com/docs/usage/webhooks/webhook-settings?web=1));
  a timeout ends the call with a brief fallback. Ask for a call with an
  explicit recipient number and purpose; the normal task approval is required
  before dialing.
