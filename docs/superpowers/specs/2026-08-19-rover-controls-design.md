# Rover drive/drill controls — design

Date: 2026-08-19
Status: approved

## Purpose

Add browser-to-Pi command sending for driving the rover (forward/back/left/right)
and moving the soil sensor probe (drill up/down). This phase delivers command
delivery only — no motor/GPIO hardware exists yet, so the Pi side stubs out the
actual actuation and just proves commands arrive reliably.

## Transport

Reuse the existing `RTCPeerConnection` already established for video (peer-to-peer
via the Metered TURN relay, offer/answer relayed through `signalling_server.py`).
Add an `RTCDataChannel` to the same connection rather than building a separate
HTTP polling path — no new signalling, no new infra, lowest latency, and it rides
the same connection already proven to connect.

- Browser (`client.html`) creates the channel with `pc.createDataChannel('control')`
  **before** calling `createOffer()`, so the data channel m-line is present in the
  initial offer.
- Pi (`broadcaster.py`) receives it via `pc.on('datachannel')` — no change needed
  to the offer/answer/ICE flow already in place.

## Message format

One small JSON object per press/release, sent over the data channel:

```json
{ "cmd": "forward", "state": "start" }
{ "cmd": "forward", "state": "stop" }
```

- `cmd` ∈ `forward | back | left | right | drill_up | drill_down`
- `state` ∈ `start | stop`

All 6 commands use the same hold-to-move model: `start` on press, `stop` on
release. No discrete-step mode, no toggle mode — one interaction model for
every command keeps both client and Pi state trivial.

## Client side (`client.html`)

- A control pad of 6 buttons is added to the existing dashboard layout.
- Each button sends `start` on `mousedown`/`touchstart` and `stop` on
  `mouseup`/`mouseleave`/`touchend` (mouseleave covers dragging off the button
  while still holding the mouse button).
- Keyboard mirrors the same handler via a shared `sendCommand(cmd, state)`
  function:
  - `W` / `ArrowUp` → forward
  - `S` / `ArrowDown` → back
  - `A` / `ArrowLeft` → left
  - `D` / `ArrowRight` → right
  - `Q` → drill_up
  - `E` → drill_down
  - `keydown` → `start`, `keyup` → `stop`.
  - A `pressed` `Set` of currently-held commands guards against OS key-repeat
    firing repeated `start` messages while a key is held down.
- On the data channel or ICE connection leaving a connected state, the client
  clears its local `pressed` set and logs it to the on-page log. There is no
  motor to actively stop yet, so this is just local state hygiene to avoid
  stale "held" state surviving a reconnect — real stop-on-disconnect handling
  belongs on the Pi side once a motor driver exists.

## Pi side (`broadcaster.py`)

- `pc.on('datachannel')` registers a handler on the incoming channel.
- Each received message is parsed as JSON and passed to `handle_command(cmd,
  state)`.
- `handle_command` is a stub: it logs `CMD {cmd} {state}` via the existing
  logger. This is the clearly-marked swap-in point for real motor/GPIO driver
  code in a later phase — no GPIO library or hardware assumption is made now.

## Out of scope (this phase)

- Real motor/GPIO/servo control — hardware and wiring aren't decided yet.
- Command authentication/authorization — matches the rest of the project's
  current single-operator, no-auth posture (see `state` dict in
  `signalling_server.py`).
- Discrete-step or toggle drive modes.
- Server-side (Render) involvement in commands — the data channel is fully
  peer-to-peer, Render only ever relayed the SDP offer/answer.

## Testing

No automated test — this phase is plumbing (data channel wiring + a logging
stub), not a logic branch worth a unit test. Verification is manual: connect
from the browser, press/release each of the 6 buttons and keys, and confirm a
matching `CMD {cmd} start` / `CMD {cmd} stop` line appears in the Pi's
console for each.
