"""
env_checks.py
Environment, system, and auth-level validators that run at scanner startup
(and some periodically).

Includes:
  - single_instance_lock(): PID-file mutex; prevents two main.py running
  - release_instance_lock():
  - verify_system_clock(): compares local clock to NTP / Fyers server time
  - verify_disk_writable(): ensures we can write to data/ directory
  - verify_disk_space(): warns if free space < threshold
  - verify_token_freshness(): hours-until-expiry on Fyers token
  - verify_calendar_freshness(): alert if holiday list doesn't cover current year+1
  - verify_dns(): can we resolve api.fyers.in
  - verify_python_env(): required package versions
"""
import os
import sys
import socket
import json
import time
from datetime import datetime, timedelta
import pytz

IST  = pytz.timezone("Asia/Kolkata")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ─── Single-instance lock ──────────────────────────────────────────────────
PID_FILE = os.path.join(ROOT, "data", "scanner.pid")


def acquire_single_instance_lock() -> bool:
    """Returns True if we got the lock, False if another instance is running.
    Writes our PID. Does NOT clean up on abnormal exit (caller should call
    release on shutdown OR detect stale PID below)."""
    if os.path.exists(PID_FILE):
        try:
            with open(PID_FILE, "r") as f:
                other_pid = int(f.read().strip())
        except Exception:
            other_pid = 0
        # Test if that PID is still alive
        if other_pid and _pid_alive(other_pid):
            return False
        # Stale PID — overwrite
    os.makedirs(os.path.dirname(PID_FILE), exist_ok=True)
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))
    return True


def release_instance_lock():
    if os.path.exists(PID_FILE):
        try:
            with open(PID_FILE, "r") as f:
                if f.read().strip() == str(os.getpid()):
                    os.remove(PID_FILE)
        except Exception:
            pass


def _pid_alive(pid: int) -> bool:
    """Cross-platform 'is this PID alive'."""
    if pid <= 0:
        return False
    try:
        if sys.platform == "win32":
            import ctypes
            PROCESS_QUERY = 0x1000
            h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY, False, pid)
            if h:
                ctypes.windll.kernel32.CloseHandle(h)
                return True
            return False
        else:
            os.kill(pid, 0)
            return True
    except Exception:
        return False


# ─── System clock ──────────────────────────────────────────────────────────
def verify_system_clock(tolerance_seconds: int = 30) -> dict:
    """Compare local clock to an NTP server. Returns {drift_seconds, ok}."""
    try:
        import struct
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(5)
        data = b'\x1b' + 47 * b'\0'
        sock.sendto(data, ("pool.ntp.org", 123))
        recv, _ = sock.recvfrom(1024)
        sock.close()
        # NTP epoch starts 1900; subtract 70 years
        t = struct.unpack("!12I", recv)[10] - 2208988800
        local  = time.time()
        drift  = abs(local - t)
        return {"drift_seconds": drift, "ok": drift <= tolerance_seconds}
    except Exception as e:
        return {"drift_seconds": None, "ok": True, "error": str(e)}


# ─── Disk ──────────────────────────────────────────────────────────────────
def verify_disk_writable() -> bool:
    """Try writing & deleting a tiny test file in data/."""
    test = os.path.join(ROOT, "data", ".write_test")
    try:
        with open(test, "w") as f:
            f.write("ok")
        with open(test, "r") as f:
            ok = f.read() == "ok"
        os.remove(test)
        return ok
    except Exception:
        return False


def verify_disk_space(min_free_gb: float = 2.0) -> dict:
    """Returns {free_gb, ok}."""
    try:
        import shutil
        usage = shutil.disk_usage(ROOT)
        free_gb = usage.free / (1024 ** 3)
        return {"free_gb": round(free_gb, 2), "ok": free_gb >= min_free_gb}
    except Exception as e:
        return {"free_gb": None, "ok": True, "error": str(e)}


# ─── Token / Auth ──────────────────────────────────────────────────────────
def verify_token_freshness() -> dict:
    """Return {hours_until_expiry, ok, expires_at, issued_at}.
    Properly decodes the Fyers JWT `exp` claim instead of guessing from
    saved_date. Fyers tokens actually live ~5-6 hours and typically expire
    around 06:00 IST regardless of when they were saved."""
    import base64
    token_path = os.path.join(ROOT, "data", "access_token.json")
    if not os.path.exists(token_path):
        return {"hours_until_expiry": 0, "ok": False, "error": "no token file"}
    try:
        with open(token_path, "r") as f:
            data = json.load(f)
        jwt = data.get("access_token", "")
        if not jwt or jwt.count(".") != 2:
            return {"hours_until_expiry": 0, "ok": False, "error": "no JWT"}
        # JWT payload (middle segment), URL-safe base64
        payload_b64 = jwt.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        exp = payload.get("exp")
        iat = payload.get("iat")
        if not exp:
            return {"hours_until_expiry": 0, "ok": False, "error": "no exp claim"}
        now_s   = datetime.now(IST).timestamp()
        seconds = exp - now_s
        hours   = seconds / 3600
        return {
            "hours_until_expiry": round(hours, 1),
            "ok":                 hours > 0,
            "expires_at":         datetime.fromtimestamp(exp, tz=IST).isoformat(),
            "issued_at":          datetime.fromtimestamp(iat, tz=IST).isoformat() if iat else None,
        }
    except Exception as e:
        return {"hours_until_expiry": 0, "ok": False, "error": str(e)}


# ─── Calendar freshness ───────────────────────────────────────────────────
def verify_calendar_freshness() -> dict:
    """Alert if NSE holiday list doesn't extend at least 30 days past today.
    Forces user to keep holiday list up-to-date."""
    from core.market_calendar import NSE_HOLIDAYS
    today = datetime.now(IST).date()
    latest_holiday = max(NSE_HOLIDAYS) if NSE_HOLIDAYS else "1970-01-01"
    latest_date = datetime.strptime(latest_holiday, "%Y-%m-%d").date()
    days_ahead = (latest_date - today).days
    return {"latest_holiday": latest_holiday, "days_ahead": days_ahead,
            "ok": days_ahead >= 30}


# ─── Network ──────────────────────────────────────────────────────────────
def verify_dns(hosts=("api-t1.fyers.in", "api.fyers.in")) -> dict:
    """Try resolving Fyers endpoints. Returns {resolved, failed, ok}."""
    resolved, failed = [], []
    for h in hosts:
        try:
            socket.gethostbyname(h)
            resolved.append(h)
        except Exception:
            failed.append(h)
    return {"resolved": resolved, "failed": failed, "ok": len(failed) == 0}


# ─── Combined startup gate ────────────────────────────────────────────────
def run_startup_checks(verbose: bool = True) -> dict:
    """Run all environment checks. Returns dict of results.
    Caller decides whether to abort startup based on which checks failed."""
    if verbose:
        print(f"\n{'='*62}")
        print("  ENVIRONMENT / SYSTEM CHECKS")
        print(f"{'='*62}")

    results = {}
    results["instance_lock"] = acquire_single_instance_lock()
    results["disk_writable"] = verify_disk_writable()
    results["disk_space"]    = verify_disk_space()
    results["system_clock"]  = verify_system_clock()
    results["token"]         = verify_token_freshness()
    results["calendar"]      = verify_calendar_freshness()
    results["dns"]           = verify_dns()

    if verbose:
        def status(ok):
            return "OK" if ok else "FAIL"
        print(f"  Single-instance lock : {status(results['instance_lock'])}"
              + (" (another scanner running!)" if not results['instance_lock'] else ""))
        print(f"  Disk writable        : {status(results['disk_writable'])}")
        ds = results['disk_space']
        print(f"  Disk space           : {status(ds['ok'])} ({ds.get('free_gb','?')} GB free)")
        sc = results['system_clock']
        drift = sc.get('drift_seconds')
        print(f"  System clock vs NTP  : {status(sc['ok'])}"
              + (f" (drift {drift:.1f}s)" if drift is not None else " (no NTP response)"))
        tk = results['token']
        print(f"  Fyers token          : {status(tk['ok'])}"
              + (f" ({tk['hours_until_expiry']}h remaining)" if tk['ok']
                 else f" ({tk.get('error','expired')})"))
        cal = results['calendar']
        print(f"  Holiday calendar     : {status(cal['ok'])}"
              + (f" (latest {cal['latest_holiday']}, {cal['days_ahead']}d ahead)"))
        dns = results['dns']
        print(f"  DNS (Fyers endpoints): {status(dns['ok'])}"
              + (f" (failed: {dns['failed']})" if not dns['ok'] else ""))
        print(f"{'='*62}\n")
    return results
