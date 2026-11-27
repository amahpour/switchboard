### Fixed

- **The meeting bot leaves a Zoom call again (#956).** Zoom's web client now opens its Leave confirmation only when Leave is activated from the keyboard, and a pointer press opens nothing, so the bot stayed in the call: it kept the meeting alive, and on an account that allows one meeting at a time, a new one couldn't start. The pinned Zoom fix (`zoom.py`) now presses Leave from the keyboard first, and switchboard waits up to 15 seconds for the leave instead of 5, so it no longer cuts the bot off halfway out.
