# Hardware commissioning

Copy `config/desk.example.toml` to `config/desk.toml`. Keep local credentials and
captured remote files out of version control. The supplied IR symbols and monitor
capabilities are examples, not captured or discovered values from your desk.

## SmartThings

Enroll both monitors in your Samsung account and verify input switching in the
SmartThings app first. Exact model numbers are still needed to establish API
support; a marketing name alone is insufficient.

For initial discovery, create a token with device read and execute scopes. Enter
the token without echoing it into shell history:

```bash
read -rs -p 'SmartThings token: ' SMARTTHINGS_TOKEN
export SMARTTHINGS_TOKEN
python3 -m desk_orchestrator smartthings devices
python3 -m desk_orchestrator smartthings device DEVICE_ID
python3 -m desk_orchestrator smartthings status DEVICE_ID
python3 -m desk_orchestrator smartthings capability CAPABILITY_ID --version 1
```

Inspect the real component, capability, command signature, attribute, and accepted
input values. Samsung devices can expose different capabilities. Switch inputs in
the app and compare status responses to identify DisplayPort and HDMI 1 values.
Copy these into `monitors.g8`; set `confirmed = true` only after verification.
`samsungvd.mediaInputSource` is only an example. The adapter supports a command
with one input argument and an attribute reporting that value. If your monitor
uses another signature or exposes no feedback attribute, extend the adapter based
on its actual capability schema rather than inventing a mapping.

The G9 currently needs no API command because it stays on DisplayPort. Its config
entry allows future scenes to use the same adapter. Command confirmation polls
SmartThings state, not the physical display, so stale cloud state is possible.

Before an input change (including a web input test), the adapter checks the
configured component's `switch` state. If it reports `off`, it sends `switch.on`
and waits for `on` before sending the input command. Power-on and input
confirmation each use `verify_timeout` (15 seconds by default). A rejected wake
command or wake timeout stops the operation. If the switch state is absent or
unknown, input switching proceeds without a wake command. Enable **Power On with
Mobile** in the monitor's network settings if needed for standby wake-up.

### Authentication for an unattended Pi

New [personal access tokens expire after 24 hours](https://developer.smartthings.com/docs/getting-started/authorization-and-permissions).
A PAT is suitable for discovery, not a durable installation. For unattended use,
register an OAuth-In App with the CLI, then use **Settings → SmartThings OAuth**
to link your account. The app supplies authorization, `/oauth/callback`, token
exchange, private storage, and refresh checks. Follow the
[OAuth setup guide](smartthings-oauth.md) for credentials, DNS/HTTPS, and testing.
Some newer API Access App documentation is preview material; choose the endpoint
matching your registered integration. A PAT alone cannot refresh itself.

The guided flow stores tokens automatically. For manual setup, store the
access/refresh token response in a private JSON file with
`access_token`, `refresh_token`, and `expires_at` (Unix seconds). If `expires_at`
is omitted, the first request refreshes immediately. Configure the absolute
`smartthings.token_file` path and client ID/secret environment variable names.
The guided flow saves `token_endpoint` automatically: `/oauth/token` for CLI
OAuth-In Apps, or `/v1/oauth/token` for the preview flow. For manual token files,
set this explicitly; older configurations without it retain `/v1/oauth/token`.
The host is fixed to `api.smartthings.com` to avoid forwarding tokens elsewhere.

The client locks refreshes across processes and atomically replaces both tokens
with mode 0600, retaining the latest single-use refresh token. If a refresh reply
is lost or the process dies between refresh and persistence, reauthorization may
be necessary. Token files here are permission-protected JSON, not encrypted by the
application; use an encrypted filesystem if encryption at rest is required.
Do not run multiple installations against the same refresh token.

Reference: [SmartThings token management](https://developer.smartthings.com/docs/service-integrations/token-management).

## Infrared learning

The web app supports **Hardware → Add & learn IR commands** for bounded raw
button capture, browsing installed keys, and single-press verification. See
[web IR learning](web-app.md#add-and-learn-ir-commands) for setup and limitations.
The manual workflow below supports protocol-aware capture with irrecord.

After enabling the ANAVI overlays as described in [Pi installation](raspberry-pi.md),
identify TX and RX devices; `/dev/lirc0` and `/dev/lirc1` numbering can vary.

```bash
sudo ir-keytable
sudo systemctl stop lircd.socket lircd.service
sudo mode2 --device /dev/lirc1
sudo irrecord --device /dev/lirc1 --disable-namespace oppo_ha1.lircd.conf
sudo irrecord --device /dev/lirc1 --disable-namespace smsl_da9.lircd.conf
```

Use the actual receiver path. Stop `mode2` before `irrecord`; follow the latter's
prompts, and ensure the remote `name` fields match `oppo_ha1` and `smsl_da9`.
Record USB, Optical, RCA, volume up/down, and the available power commands. If a
protocol cannot be decoded, follow irrecord's raw-mode procedure. No IR hex codes
are fabricated in this repository.

Install the resulting files under `/etc/lirc/lircd.conf.d/`. Set lircd's `device`
to the transmitter, then restart:

```bash
sudo systemctl start lircd.socket lircd.service
irsend --device /run/lirc/lircd LIST '' ''
irsend --device /run/lirc/lircd LIST oppo_ha1 ''
irsend --device /run/lirc/lircd SEND_ONCE oppo_ha1 KEY_USB
```

The final command is a real transmission; use only your verified learned key.
Test each input from a different input, and each power command from both on and
off. A learned toggle cannot become a discrete command by renaming it. If your
hardware has no discrete on/off codes, obtain a supported alternative or add
measured power-state feedback before enabling unattended power control.

Update each config command's `sequence` and set `verified = true` only for the
tested result. `discrete = true` asserts a verified physical behavior; it does not
change the waveform. Confirm line of sight from the fixed Pi position.

## Volume

The HA-1 analog volume mechanism is documented by
[OPPO](https://www.oppodigital.com/KnowledgeBase.aspx?KBID=91&ProdID=HA-1).
Do not implement `target minus remembered dB divided by step size`: there is no
reliable fixed step, and manual adjustments or missed IR invalidate a counter.

The default is to reject volume automation. To accept a measured approximation:

1. Disconnect headphones and disable speaker amplification. Ensure the HA-1 is
   using variable preamp output, not a bypass mode. Confirm the DA-9's independent
   level setting and RCA signal chain.
2. With visual observation, measure a finite sequence of volume-down presses
   that reliably reaches the same minimum reference from any intended position.
3. Measure individual volume-up presses and timing needed to approach −32.0 dB.
   Repeat from varied starting positions and after power cycles. There is no
   supplied default count because that would be an unverified physical value.
4. Repeat separately for the explicitly requested +6.0 dB target. Restore speaker
   amplification only after visually checking the target during commissioning.
5. Store each full reference-then-target sequence, mark `calibrated` and
   `starts_from_known_reference` true, and set `allow_approximate_volume = true`.

Sequence entries are `{ key = "LEARNED_KEY", count = N, gap = SECONDS }`;
count is 1–300, gap 0.02–5 seconds. Each entry sends bounded `SEND_ONCE` calls.
The adapter never uses a continuous repeating command. LIRC success means a
transmission was accepted, not that the HA-1 received it. An obstructed receiver
can invalidate even a calibrated sequence. This remains an approximation, not a
closed-loop volume setter.

If exact front-panel −32.0/+6.0 readings are mandatory, leave approximation
disabled and add display feedback hardware plus a bounded feedback controller.
An initial software-only project cannot remove that physical limitation.

## Acceptance sequence

1. Validate each component independently: monitor app/API, learned IR commands,
   KVM hotkeys from the Pi, and Linux numpad key events. Check that KVM USB hub
   switching follows the selected computer so the HA-1 USB connection follows.
2. Confirm the numpad path and proposed Steam Deck audio settings. A USB-C dock
   and suitable video adapter for Steam Deck are assumed to be in place.
3. Run `desk -c config/desk.toml check SCENE --probe`. Resolve every blocker.
4. Run `desk -c config/desk.toml plan SCENE`, then `run SCENE --live` while
   watching each device. A runtime failure can leave a partially applied scene.
5. Verify all five modes, repeated scene selection, manual changes between
   scenes, unplugged numpad/reconnection, loss of internet, and IR obstruction.
   Verify headphones separately; powering down the DA-9 does not mute headphones.
6. Enable the systemd numpad service only after supervised acceptance.

Exact physical verification must be done at the desk; unit tests cover software
behavior and cannot certify line of sight, monitor support, or volume accuracy.
