# OpenComputer SMS channel (Twilio)

Two-way SMS via Twilio. Inbound: aiohttp webhook receives Twilio POSTs.
Outbound: Twilio REST API.

## Prerequisites

1. A Twilio account with an SMS-capable phone number.
2. A way to expose your local webhook port to the public Internet
   (ngrok, Cloudflare Tunnel, your own VPS).

## Required env vars

```bash
export TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
export TWILIO_AUTH_TOKEN=your_auth_token_here
export TWILIO_PHONE_NUMBER=+15551234567   # E.164; the Twilio-owned number
export SMS_WEBHOOK_URL=https://your.public.url/webhooks/twilio
```

## Optional env vars

```bash
export SMS_WEBHOOK_PORT=8080            # local listen port (default 8080)
export SMS_WEBHOOK_HOST=0.0.0.0         # bind host (default all interfaces)
export SMS_INSECURE_NO_SIGNATURE=true   # SKIP signature validation
                                        # (DEV ONLY — never in production)
```

## Twilio configuration

In the Twilio console, set your phone number's "A MESSAGE COMES IN"
webhook to `${SMS_WEBHOOK_URL}` with HTTP POST.

## Security

Inbound webhooks are validated against the `X-Twilio-Signature` header
(HMAC-SHA1 over URL + sorted form params). The adapter REFUSES to start
if neither `SMS_WEBHOOK_URL` is configured nor `SMS_INSECURE_NO_SIGNATURE=true`
is set — there is no silent insecure default.

## Limitations

- Text only. Inbound MMS media is ignored.
- 1600-char cap per send (~10 SMS segments). Longer replies chunk on
  line boundaries.
- Cron / scheduled-message routing through SMS not implemented in this
  adapter; configure in the cron plugin if needed.
