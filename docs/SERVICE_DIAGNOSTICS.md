# Service diagnostics contract

Remote Host MCP keeps the historical `available` field for compatibility: it indicates whether the `systemctl` executable is present. It does **not** by itself mean that a systemd manager is operational.

`service_status` also reports additive capability fields:

- `systemctl_present`: the `systemctl` executable is available.
- `pid1_is_systemd`: whether `/proc/1/comm` identifies PID 1 as systemd, or `null` when it cannot be inspected.
- `manager_reachable`: a read-only `systemctl show` probe can reach the local system manager.
- `operational`: Remote Host MCP may attempt exact `systemctl` service actions in this environment.
- `reason`: stable dependency/capability reason when the manager is not operational, such as `systemctl_missing`, `manager_unreachable_pid1_not_systemd`, `manager_unreachable`, `manager_probe_timeout`, or `manager_probe_failed`.

`service_action` performs the same manager preflight before `start`, `stop`, or `restart`. When the manager is unavailable, it fails closed and returns the structured status without invoking an action verb. It does not fall back to shell commands, supervisors, fuzzy process matching, `pkill`, or `killall`.

Positive `service_action` integration is tested only against a uniquely named disposable transient unit on a systemd-capable disposable runner. A non-systemd container/host is a valid environment limitation and must not be made to pass by operating an unrelated production service.
