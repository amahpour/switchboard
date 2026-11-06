### Fixed

`meeting-host stop`, a failed join and a lost joinly now tell the broker at once. The room's call bar
ends immediately, with a fixed notice explaining why, and the call key stops working. If the broker
cannot be reached, the existing silence timeout remains the fallback.

Only that call's own key opens the departure route. It can end only its own call.

### Upgrading

Update the server and relay image together. Older relays still use the silence timeout.
The host scripts and skills now describe immediate departure; Claude is 0.14.2 and Codex 0.5.1.
