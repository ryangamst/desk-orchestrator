# Connect SmartThings with OAuth

The app supports account linking, refresh on API requests, and an explicit refresh
test. Register an OAuth-In App using the CLI as described in the
[SmartThings quick start](https://developer.smartthings.com/docs/getting-started/quickstart).
It needs `r:devices:*` and `x:devices:*` permissions.

## Credentials and callback address

Set `SMARTTHINGS_CLIENT_ID` and `SMARTTHINGS_CLIENT_SECRET` in the environment
file loaded by the web service, then restart it. The local service uses its
`EnvironmentFile` override (currently `.runtime/web/credentials.env`); the Pi
services use `/etc/desk-orchestrator/credentials.env`. A generic `.env` file is
not loaded automatically. The CLI or numpad service also needs those credentials
and the same managed configuration/token file for OAuth device control.

On the Pi, HTTP, HTTPS, and the managed numpad service all use the same
`/etc/desk-orchestrator/credentials.env`. Restart each running service after
changing that file. The **loaded (not verified)** status only confirms the two
variables exist; the token exchange verifies the pair with SmartThings. A 401
during that exchange identifies client authentication, not monitor input mapping.

Register an exact redirect URI ending in `/oauth/callback`, such as
`https://desk.example.com/oauth/callback`. Your browser must resolve and
reach this address. The hostname must serve this installation of the app,
including `/settings`, `/login`, and `/oauth/callback`. Registration does not
configure DNS, a certificate, or a reverse proxy.

The built-in HTTPS service needs no proxy. If using a reverse proxy instead,
configure it to preserve the original `Host` header when
forwarding to the loopback web service. The saved callback hostname is accepted
by the app's host validation; saving it does not change the listening address
or expose the service publicly. The app does not trust arbitrary forwarded
headers. When serving exclusively through HTTPS, start with `--secure-cookie`.
Configure proxy access logs to omit callback query strings, which contain
short-lived authorization codes.

HTTP redirects are supported only for `localhost`, `127.0.0.1`, or `::1`
development. Whether SmartThings accepts a given registered URI must be verified
there. HTTPS URLs need a certificate trusted by the browser.

## Connect and test

1. In **Settings → SmartThings OAuth**, enter the registered redirect URI.
2. Choose **CLI OAuth-In App (/oauth/token)** for `smartthings apps:create`.
   Select the `/v1/oauth/token` preview option only for an integration using it.
3. Click **Save OAuth setup**. The current PAT or OAuth connection remains active.
4. Open Settings using the callback's hostname and port and sign in there. For
   the example, use `https://desk.example.com/settings`. Starting from
   localhost and returning to another host loses the session, so the app rejects
   that start with an explanation.
5. Click **Connect to SmartThings**. Samsung authorization opens in the same tab;
   if automatic navigation is unavailable, use **Continue to SmartThings**.
   Sign in to Samsung and approve access. Finish within ten minutes. Starting another flow replaces the pending flow.
6. The callback exchanges the code and saves a private JSON file under the runtime
   directory's `oauth/` folder. It activates the file only after receiving valid
   tokens and saving the configuration. Manual token copying is unnecessary.
7. Click **Check OAuth connection**, then **Test token refresh**. The latter
   rotates and saves tokens and reads the device list. Neither check sends device
   commands. Then use the G8's input-test buttons for physical testing.

The callback validates random state, the initiating browser login, expiry,
registered host, and unchanged connection settings and client credentials.
Pending state is consumed before exchanging the single-use code. Denial,
timeout, bad tokens, or a failed save leaves the previous connection active.
Errors return to Settings without showing codes or tokens. Cookies use
`SameSite=Lax` to retain login on Samsung's top-level GET redirect; all local
POST actions still require CSRF tokens.

## Ongoing operation

Refresh occurs when a request finds the token within two minutes of expiry, or
after a 401 (one retry). There is no background refresh timer. Extended downtime,
revocation, or refresh-token expiry can require reconnecting. **Test token
refresh** forces refresh even when the access token has not expired.

Token files use mode 0600 and atomic writes. They are permission-protected, not
encrypted by the app. Backups contain references, not token or secret values.
Reconnection retains old token files but activates only the new one. Do not run
desktop and Pi independently using copied rotating token pairs. Authorize the
intended installation or transfer it with the previous instance stopped and its
local paths adjusted.

Source deployments deliberately exclude the desktop's runtime configuration and
secrets. Set up the Pi's HTTPS hostname/certificate and OAuth client
environment locally. Authorize OAuth on the Pi after its DNS and callback are
ready. A normal Pi update preserves its existing OAuth/TLS state. Importing the
web configuration backup preserves the target's local credential references;
it does not transfer the desktop token pair or certificate keys.

This implementation polls SmartThings. It does not receive event webhooks or
automatically delete tokens when unlinked on Samsung's side. The existing device
discovery and command flow does not require an incoming webhook.

For a private hostname with AWS Lightsail DNS, use the built-in [Let’s Encrypt HTTPS setup](https-lightsail.md).
