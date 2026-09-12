from __future__ import annotations

from pathlib import Path

TRANSFORMER = Path("scripts/apply_audit_closure_20260913.py")
text = TRANSFORMER.read_text(encoding="utf-8")
old_probe = '''        systemd = _systemd_manager_capability()
        state_parent = settings.state_dir if settings.state_dir.exists() else settings.state_dir.parent
'''
new_probe = '''        systemctl_present, pid1_is_systemd, manager_reachable, reason, _probe_code, _probe_output = (
            _systemd_manager_capability()
        )
        state_parent = settings.state_dir if settings.state_dir.exists() else settings.state_dir.parent
'''
old_fields = '''                    "systemctl_present": systemd.systemctl_present,
                    "pid1_is_systemd": systemd.pid1_is_systemd,
                    "manager_reachable": systemd.manager_reachable,
                    "operational": systemd.operational,
                    "reason": systemd.reason,
'''
new_fields = '''                    "systemctl_present": systemctl_present,
                    "pid1_is_systemd": pid1_is_systemd,
                    "manager_reachable": manager_reachable,
                    "operational": manager_reachable,
                    "reason": reason,
'''
if text.count(old_probe) != 1 or text.count(old_fields) != 1:
    raise SystemExit("doctor transformer patch precondition failed")
text = text.replace(old_probe, new_probe, 1).replace(old_fields, new_fields, 1)
TRANSFORMER.write_text(text, encoding="utf-8")
Path(__file__).unlink(missing_ok=True)
