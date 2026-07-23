# Tailscale Development Access

This guide documents the current phone-access setup for Agency when the frontend is opened through a Wi-Fi LAN address
or a Tailscale address while the stack is still running as a local development environment.

## Current Topology

Local development uses two repositories side by side:

```text
Repository/
  Agency-BE/
  open-agency-fe/
```

Start Open Agency from the backend repository:

```bash
./run-windows.sh start
```

The script:

- starts the backend Docker Compose stack
- writes `../open-agency-fe/.env.local`
- starts the frontend with `next dev -H 0.0.0.0`
- explicitly enables the local-only frontend `/backend/*` rewrite for phone/Tailscale testing

In this mode the phone should open the frontend, not the backend directly:

```text
http://<windows-lan-ip>:3000
http://<tailscale-ip-or-magicdns-name>:3000
```

The backend health check should work through the frontend proxy:

```text
http://<same-frontend-host>:3000/backend/health
```

The frontend server talks to the backend from the Windows machine using:

```env
AGENCY_INTERNAL_API_BASE_URL=http://127.0.0.1:8000
AGENCY_FE_ENABLE_BACKEND_REWRITE=true
NEXT_PUBLIC_AGENCY_API_BASE_URL=/backend
LOCAL_BACKEND=/backend
```

That means the browser does not need direct access to backend port `8000`; it only needs frontend port `3000`. The
rewrite is a trusted local-development fallback and must not be enabled for production or internet-facing deployments.

## Tailscale Auth Rule

When the app is reachable through more than one host, such as Wi-Fi LAN IP and Tailscale IP, do not pin Auth.js to one
host in local development.

Use:

```env
AUTH_TRUST_HOST=true
```

Leave these unset unless you intentionally want exactly one canonical URL:

```env
AUTH_URL=
NEXTAUTH_URL=
```

`run-windows.sh start` removes `AUTH_URL` and `NEXTAUTH_URL` by default and keeps `AUTH_TRUST_HOST=true`. This lets
Auth.js use the actual request host. Without this, the login page may load over Tailscale but the credentials callback
can redirect or set callback cookies for the Wi-Fi host, `0.0.0.0`, or another host the phone is not using.

## Tailscale Checklist

1. Make sure the Windows machine and phone are both connected to the same tailnet.
2. Start Open Agency from Git Bash or WSL:

```bash
./run-windows.sh start
```

3. Open the frontend through the Tailscale address or MagicDNS name:

```text
http://<tailscale-ip-or-magicdns-name>:3000
```

4. Check backend reachability through the frontend:

```text
http://<tailscale-ip-or-magicdns-name>:3000/backend/health
```

Expected response:

```json
{"ok": true}
```

5. Log in with the local development credentials from `../open-agency-fe/.env.local`.

Default local credentials are:

```text
Email: dev@example.com
Password: change-me
```

If the login page loads but sign-in fails only over mobile data, clear browser site data for the Tailscale host and
restart the frontend. Stale auth cookies from the LAN host can confuse the callback flow.

## Troubleshooting

Check the generated frontend environment:

```bash
./run-windows.sh status
```

Healthy local/Tailscale development should show:

```env
AUTH_TRUST_HOST=true
NEXT_PUBLIC_AGENCY_DEV_AUTH_ENABLED=true
AGENCY_FE_ENABLE_BACKEND_REWRITE=true
NEXT_PUBLIC_AGENCY_API_BASE_URL=/backend
LOCAL_BACKEND=/backend
AGENCY_INTERNAL_API_BASE_URL=http://127.0.0.1:8000
```

It should not show `AUTH_URL` or `NEXTAUTH_URL` unless `AGENCY_PUBLIC_APP_URL` was intentionally set.

If the phone can see the login page but cannot sign in:

- confirm the phone is opening the same host for the page and the login callback
- remove stale `AUTH_URL` and `NEXTAUTH_URL` from `../open-agency-fe/.env.local`
- restart the frontend so Next.js reloads `.env.local`
- clear site data for the Tailscale host on the phone
- verify `/backend/health` through the same host

If the phone cannot load the page at all:

- confirm the frontend is listening on `0.0.0.0:3000`
- confirm Tailscale is connected on both devices
- confirm the phone can reach other services on the Windows Tailscale address
- check local firewall rules for inbound TCP `3000`

## Production Without Tailscale

For production, do not carry over the local Tailscale development assumptions. Production should have a single canonical
HTTPS application URL and real authentication.

Change the frontend deployment to:

- build and run with `next build` and `next start`, or deploy through a production Next.js platform
- serve behind HTTPS
- set a canonical public URL:

```env
AUTH_URL=https://app.example.com
NEXTAUTH_URL=https://app.example.com
AUTH_TRUST_HOST=true
```

- set a strong secret:

```env
AUTH_SECRET=<strong-random-secret>
NEXTAUTH_SECRET=<same-or-rotated-secret-if-still-required>
```

- disable local development auth:

```env
NEXT_PUBLIC_APP_ENV=production
NEXT_PUBLIC_AGENCY_DEV_AUTH_ENABLED=false
```

- configure the production identity provider, such as Azure AD or Google OAuth
- use production callback URLs registered with that provider
- remove default dev credentials from deployed environments

Change backend routing to:

- prefer the public API origin, for example `NEXT_PUBLIC_AGENCY_API_BASE_URL=https://api.example.com`
- set `AGENCY_ALLOWED_ORIGINS` on the backend to the exact `open-agency-fe` origin, for example `https://app.example.com`
- do not enable `AGENCY_FE_ENABLE_BACKEND_REWRITE` in production or internet-facing deployments
- set `AGENCY_INTERNAL_API_BASE_URL` only for explicit server-side BFF routes that need a private backend service URL
- expose the backend only through the production ingress or API gateway, not directly from a developer laptop

Change network and security to:

- do not rely on Tailscale membership as the user authentication boundary
- restrict admin/backend ports with firewall or private networking
- move secrets into the deployment secret manager
- use production database, Redis, and object storage credentials
- enable production logging, metrics, backups, and alerting

Tailscale can still be useful in production for operator access, private admin panels, or service-to-service networking,
but the public user-facing app should not depend on a developer machine's tailnet address.
