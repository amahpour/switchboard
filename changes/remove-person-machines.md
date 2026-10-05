### Fixed

- **Security: removing a person now removes the machines they paired or approved (#178).** Before, someone you removed was signed out, but a machine they had paired kept dialing in, and its agents could still join every room. Now the machine goes with them: its key is forgotten and its connection refused. The Machines sheet and the notices say who paired and who approved each machine, instead of always naming the admin.
