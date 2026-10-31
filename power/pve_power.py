#!/usr/bin/env python3
"""
pve_power.py: estimate the wall (AC) power draw of a Proxmox host.

Model
  CPU     measured: Intel RAPL package energy (includes the iGPU), plus motherboard VRM loss
  RAM     per-DIMM idle/active watts, scaled by CPU utilisation
  Drives  per-drive idle/active/standby watts from datasheets, scaled by /proc/diskstats busy time
  Fixed   motherboard/chipset, NIC, WiFi, fans, extras
  PSU     AC input interpolated from the Cybenetics test of the Corsair RM650e (ATX 3.1) at 230V
  Guests  CPU power split across LXCs/VMs by their share of CPU time (cgroup v2)
  Cal     wall = raw_estimate * CAL_SCALE + CAL_OFFSET  (see README)

Apple Silicon (Asahi Linux) has no RAPL, but its SMC measures power itself
(macsmc_hwmon: AC input, total system, SoC "heatpipe"), so there it reports
those readings instead, plus the USB-C charger's loss (CHARGER_EFFICIENCY).

Usage (as root, RAPL counters are root-only)
  pve_power.py                    live view in the terminal
  pve_power.py --once --json      one sample as JSON
  pve_power.py --serve 9105       HTTP: /json (Home Assistant) and /metrics (Prometheus)

Python 3 standard library only.
"""

import argparse
import datetime as dt
import glob
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------------------------------------------------------------------
# Configuration: tuned for i5-12600K / MSI PRO B760M-A WIFI / RM650e (2025)
# ---------------------------------------------------------------------------

INTERVAL_S = 5
RAPL_PKG = "/sys/class/powercap/intel-rapl:0"
VRM_EFFICIENCY = 0.88          # 12V EPS -> Vcore conversion on the board

DIMMS = 4                      # 4x 8GB DDR4
DIMM_IDLE_W = 0.9
DIMM_ACTIVE_W = 2.5

# Matched against /sys/block/<dev>/device/model (substring). Watts from datasheets.
DRIVE_SPECS = {
    "ST2000DM006": {"idle": 5.4, "active": 8.0, "standby": 0.75},   # Seagate BarraCuda 2TB 7200rpm
    "MZVLW256HEHP": {"idle": 0.45, "active": 6.1, "standby": 0.45},  # Samsung PM961 256GB
}
GENERIC_HDD = {"idle": 5.0, "active": 7.0, "standby": 0.8}
GENERIC_SSD = {"idle": 0.5, "active": 4.0, "standby": 0.5}

FIXED_W = {
    "motherboard": 6.0,          # B760 PCH, Super I/O, audio, USB controllers, DRAM VRM
    "nic_2_5gbe": 1.0,           # Realtek RTL8125, link up
    "wifi": 0.3,                 # CNVi module, idle
    "cpu_cooler_fan": 1.5,
    "case_fans": 3 * 1.2,        # <-- set to your case fan count
    "extras": 0.0,               # USB dongles, RGB, etc.
}

# Corsair RM650e (ATX 3.1), Cybenetics report CR65002628, 230V. (DC out W, AC in W)
PSU_CURVE = [
    (20.0, 28.05), (40.0, 48.57), (60.0, 69.25), (65.0, 74.94), (80.0, 89.99),
    (130.0, 142.76), (195.0, 211.08), (260.0, 280.19), (325.1, 350.24),
    (389.3, 420.23), (454.8, 492.31), (519.6, 564.73), (585.0, 638.50), (649.8, 713.03),
]

CAL_SCALE = 1.0                # set after comparing with a wall meter
CAL_OFFSET = 0.0

# Apple Silicon: the SMC measures DC power at the USB-C port; the brick's own loss
# isn't visible, so wall = AC input / this. Set after comparing with a wall meter.
CHARGER_EFFICIENCY = 0.85

STATE_FILE = "/var/lib/pve-power/state.json"   # cumulative kWh survives restarts
PRICE_PER_KWH = None           # e.g. 0.30 to show cost per day; None hides it
STANDBY_CHECK_S = 60           # how often to ask the HDD whether it is spun down
GUEST_NAME_REFRESH_S = 300

SKIP_DEV_PREFIXES = ("loop", "ram", "dm-", "zd", "sr", "md", "nbd", "rbd")

# ---------------------------------------------------------------------------


def read_text(path):
    with open(path) as f:
        return f.read().strip()


def read_int(path):
    return int(read_text(path))


class Rapl:
    """Package + sub-domain power from powercap energy counters."""

    def __init__(self, base):
        if not os.path.exists(os.path.join(base, "energy_uj")):
            raise SystemExit(f"RAPL not found at {base} (is intel_rapl loaded?)")
        self.paths = {"package": base}
        for sub in sorted(glob.glob(base + ":*")):
            self.paths[read_text(os.path.join(sub, "name"))] = sub   # core, uncore
        self.max = {k: read_int(os.path.join(p, "max_energy_range_uj")) for k, p in self.paths.items()}
        self.last = self._read()

    def _read(self):
        return {k: read_int(os.path.join(p, "energy_uj")) for k, p in self.paths.items()}

    def sample(self, dt_s):
        now = self._read()
        out = {}
        for k, e in now.items():
            d = e - self.last[k]
            if d < 0:                       # counter wrapped
                d += self.max[k]
            out[k] = d / 1e6 / dt_s
        self.last = now
        return out


def smc_sensors():
    """Apple SMC power sensors (watts by label), or None when this isn't a Mac."""
    for h in glob.glob("/sys/class/hwmon/hwmon*"):
        try:
            if read_text(os.path.join(h, "name")) != "macsmc_hwmon":
                continue
            out = {}
            for f in glob.glob(os.path.join(h, "power*_input")):
                out[read_text(f.replace("_input", "_label"))] = read_int(f) / 1e6
            return out if "AC Input Power" in out else None
        except (OSError, ValueError):
            continue
    return None


def read_cpu_jiffies():
    with open("/proc/stat") as f:
        vals = [int(v) for v in f.readline().split()[1:]]
    total = sum(vals[:8])                   # guest time is already inside user/nice
    idle = vals[3] + vals[4]                # idle + iowait
    return total, total - idle


def read_io_ticks():
    out = {}
    with open("/proc/diskstats") as f:
        for line in f:
            p = line.split()
            out[p[2]] = int(p[12])          # ms spent doing I/O
    return out


def discover_drives():
    drives = {}
    for path in sorted(glob.glob("/sys/block/*")):
        dev = os.path.basename(path)
        if dev.startswith(SKIP_DEV_PREFIXES):
            continue
        model = ""
        for mp in (f"{path}/device/model", f"{path}/device/device/model"):
            if os.path.exists(mp):
                model = read_text(mp)
                break
        try:
            rotational = read_int(f"{path}/queue/rotational") == 1
        except OSError:
            rotational = False
        spec, source = None, "datasheet"
        for key, s in DRIVE_SPECS.items():
            if key in model:
                spec = s
                break
        if spec is None:
            spec, source = (GENERIC_HDD if rotational else GENERIC_SSD), "generic"
        drives[dev] = {"model": model, "rotational": rotational, "spec": spec, "source": source}
    return drives


def hdd_in_standby(dev):
    """True/False, or None if it can't tell. Neither command spins the drive up."""
    for cmd in (["hdparm", "-C", f"/dev/{dev}"], ["smartctl", "-n", "standby", "-i", f"/dev/{dev}"]):
        if shutil.which(cmd[0]) is None:
            continue
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout.lower()
        except Exception:
            continue
        if cmd[0] == "hdparm":
            if "standby" in out or "sleeping" in out:
                return True
            if "active" in out or "idle" in out:
                return False
        else:
            return "standby mode" in out or "sleep mode" in out
    return None


def read_cgroup_usec(path):
    with open(path) as f:
        for line in f:
            if line.startswith("usage_usec"):
                return int(line.split()[1])
    return 0


def read_guests():
    g = {}
    for path in glob.glob("/sys/fs/cgroup/lxc/*/cpu.stat"):
        vmid = path.split("/")[-2]
        if vmid.isdigit():
            g[("lxc", vmid)] = read_cgroup_usec(path)
    for path in glob.glob("/sys/fs/cgroup/qemu.slice/*.scope/cpu.stat"):
        vmid = path.split("/")[-2].removesuffix(".scope")
        if vmid.isdigit():
            g[("qemu", vmid)] = read_cgroup_usec(path)
    return g


def guest_name(kind, vmid):
    conf, key = ((f"/etc/pve/lxc/{vmid}.conf", "hostname:") if kind == "lxc"
                 else (f"/etc/pve/qemu-server/{vmid}.conf", "name:"))
    try:
        with open(conf) as f:
            for line in f:
                if line.startswith("["):       # snapshot sections
                    break
                if line.startswith(key):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return f"{kind}-{vmid}"


def psu_ac(dc_w):
    """Interpolate PSU loss (AC - DC) from the test curve."""
    pts = PSU_CURVE
    if dc_w <= pts[0][0]:
        return dc_w + (pts[0][1] - pts[0][0])
    for (d0, a0), (d1, a1) in zip(pts, pts[1:]):
        if dc_w <= d1:
            l0, l1 = a0 - d0, a1 - d1
            return dc_w + l0 + (l1 - l0) * (dc_w - d0) / (d1 - d0)
    d, a = pts[-1]
    return dc_w * a / d


class Estimator:
    def __init__(self):
        self.clk_tck = os.sysconf("SC_CLK_TCK")
        self.rapl = Rapl(RAPL_PKG)
        self.drives = discover_drives()
        self.standby = {d: None for d, v in self.drives.items() if v["rotational"]}
        self.standby_checked = 0.0
        self.names, self.names_t = {}, 0.0
        self.energy_wh = self._load_energy()
        self.last_save = time.monotonic()
        self.last_t = time.monotonic()
        self.last_cpu = read_cpu_jiffies()
        self.last_io = read_io_ticks()
        self.last_guests = read_guests()

    # -- persistence --
    def _load_energy(self):
        try:
            with open(STATE_FILE) as f:
                return float(json.load(f).get("energy_wh", 0.0))
        except (OSError, ValueError):
            return 0.0

    def save_energy(self):
        try:
            os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
            tmp = STATE_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"energy_wh": self.energy_wh}, f)
            os.replace(tmp, STATE_FILE)
        except OSError as e:
            print(f"warning: could not save state: {e}", file=sys.stderr)

    # -- one measurement over the time since the previous call --
    def sample(self):
        now = time.monotonic()
        dt_s = max(now - self.last_t, 1e-3)
        self.last_t = now

        rapl = self.rapl.sample(dt_s)

        total, busy = read_cpu_jiffies()
        d_total, d_busy = total - self.last_cpu[0], busy - self.last_cpu[1]
        self.last_cpu = (total, busy)
        cpu_util = d_busy / d_total if d_total > 0 else 0.0

        io = read_io_ticks()
        if self.standby and now - self.standby_checked >= STANDBY_CHECK_S:
            for dev in self.standby:
                self.standby[dev] = hdd_in_standby(dev)
            self.standby_checked = now

        comps = {}
        pkg = rapl.get("package", 0.0)
        comps["cpu_package"] = pkg
        comps["vrm_loss"] = pkg / VRM_EFFICIENCY - pkg
        comps["ram"] = DIMMS * (DIMM_IDLE_W + (DIMM_ACTIVE_W - DIMM_IDLE_W) * cpu_util)

        drive_info = {}
        for dev, d in self.drives.items():
            ticks = io.get(dev, 0) - self.last_io.get(dev, 0)
            util = min(max(ticks / (dt_s * 1000), 0.0), 1.0)
            spec = d["spec"]
            asleep = self.standby.get(dev) is True and util == 0
            if util > 0 and dev in self.standby:
                self.standby[dev] = False
            w = spec["standby"] if asleep else spec["idle"] + (spec["active"] - spec["idle"]) * util
            comps[f"drive_{dev}"] = w
            drive_info[dev] = {"model": d["model"], "util": round(util, 3),
                               "standby": asleep, "spec": d["source"]}
        self.last_io = io

        comps.update(FIXED_W)

        dc = sum(comps.values())
        ac = psu_ac(dc)
        comps["psu_loss"] = ac - dc
        wall = ac * CAL_SCALE + CAL_OFFSET

        # energy
        self.energy_wh += wall * dt_s / 3600
        if now - self.last_save >= 60:
            self.save_energy()
            self.last_save = now

        # guests: wall-side CPU power split by CPU time share
        guests_now = read_guests()
        if now - self.names_t >= GUEST_NAME_REFRESH_S:
            self.names = {k: guest_name(*k) for k in guests_now}
            self.names_t = now
        busy_usec = d_busy * 1e6 / self.clk_tck
        cpu_wall = (pkg / VRM_EFFICIENCY) * (ac / dc) * CAL_SCALE if dc > 0 else 0.0
        guests = []
        for key, usec in guests_now.items():
            prev = self.last_guests.get(key)
            if prev is None or busy_usec <= 0:
                continue
            share = min(max((usec - prev) / busy_usec, 0.0), 1.0)
            kind, vmid = key
            name = self.names.get(key) or guest_name(kind, vmid)
            guests.append({"vmid": vmid, "type": kind, "name": name,
                           "cpu_share": round(share, 4), "cpu_w": round(cpu_wall * share, 2)})
        self.last_guests = guests_now
        guests.sort(key=lambda g: g["cpu_w"], reverse=True)

        result = {
            "timestamp": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "wall_w": round(wall, 1),
            "wall_raw_w": round(ac, 1),
            "dc_load_w": round(dc, 1),
            "psu_efficiency": round(dc / ac, 3) if ac else None,
            "cpu_util": round(cpu_util, 3),
            "cpu_core_w": round(rapl.get("core", 0.0), 2),
            "igpu_w": round(rapl.get("uncore", 0.0), 2),
            "energy_kwh": round(self.energy_wh / 1000, 4),
            "components": {k: round(v, 2) for k, v in comps.items()},
            "drives": drive_info,
            "guests": guests,
        }
        if PRICE_PER_KWH is not None:
            result["cost_per_day"] = round(wall * 24 / 1000 * PRICE_PER_KWH, 2)
        return result


class SmcEstimator(Estimator):
    """Apple Silicon: measured, not modelled. Shares energy persistence."""

    def __init__(self):
        self.energy_wh = self._load_energy()
        self.last_save = self.last_t = time.monotonic()
        self.last_cpu = read_cpu_jiffies()

    def sample(self):
        now = time.monotonic()
        dt_s = max(now - self.last_t, 1e-3)
        self.last_t = now
        total, busy = read_cpu_jiffies()
        d_total, d_busy = total - self.last_cpu[0], busy - self.last_cpu[1]
        self.last_cpu = (total, busy)

        w = smc_sensors() or {}
        ac_in = w.get("AC Input Power", 0.0)
        system = w.get("Total System Power", 0.0)
        soc = w.get("Heatpipe Power", 0.0)
        wall = ac_in / CHARGER_EFFICIENCY * CAL_SCALE + CAL_OFFSET if ac_in > 0 else 0.0
        comps = {"soc": soc, "rest_of_system": max(system - soc, 0.0)}
        if ac_in - system > 0.5:   # below that it is just the two sensors read a moment apart
            comps["battery_charging"] = ac_in - system
        if ac_in > 0:
            comps["charger_loss"] = ac_in / CHARGER_EFFICIENCY - ac_in

        self.energy_wh += wall * dt_s / 3600
        if now - self.last_save >= 60:
            self.save_energy()
            self.last_save = now
        return {
            "timestamp": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "wall_w": round(wall, 1),
            "wall_raw_w": round(ac_in, 1),
            "dc_load_w": round(system, 1),
            "psu_efficiency": None,       # an assumption here, not a curve: don't show it
            "on_battery": ac_in <= 0,
            "cpu_util": round(d_busy / d_total, 3) if d_total > 0 else 0.0,
            "cpu_core_w": round(soc, 2),
            "igpu_w": 0.0,
            "energy_kwh": round(self.energy_wh / 1000, 4),
            "components": {k: round(v, 2) for k, v in comps.items()},
            "drives": {},
            "guests": [],
        }


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def render_terminal(r):
    lines = [
        f"Proxmox power estimate   {r['timestamp']}",
        "",
        f"  WALL (est.)   {r['wall_w']:7.1f} W     DC load {r['dc_load_w']:.1f} W, "
        f"PSU eff {(r['psu_efficiency'] or 0) * 100:.1f}%",
        f"  CPU util      {r['cpu_util'] * 100:6.1f} %     cores {r['cpu_core_w']:.1f} W, iGPU {r['igpu_w']:.1f} W",
        f"  Energy total  {r['energy_kwh']:8.3f} kWh",
    ]
    if "cost_per_day" in r:
        lines.append(f"  At this rate  ${r['cost_per_day']:.2f}/day")
    lines += ["", "  Breakdown"]
    for k, v in sorted(r["components"].items(), key=lambda kv: -kv[1]):
        lines.append(f"    {k:<18}{v:7.2f} W")
    lines += ["", "  Drives"]
    for dev, d in r["drives"].items():
        state = "standby" if d["standby"] else f"{d['util'] * 100:5.1f}% busy"
        lines.append(f"    {dev:<10}{d['model'][:28]:<30}{state}  ({d['spec']})")
    if r["guests"]:
        lines += ["", "  Guests (CPU share of wall power)"]
        for g in r["guests"][:15]:
            lines.append(f"    {g['type']:<5}{g['vmid']:>5}  {g['name'][:24]:<26}{g['cpu_w']:6.2f} W")
    return "\033[H\033[J" + "\n".join(lines)


def _esc(v):
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def render_prometheus(r):
    out = [
        "# HELP pve_power_wall_watts Estimated wall (AC) power draw",
        "# TYPE pve_power_wall_watts gauge",
        f"pve_power_wall_watts {r['wall_w']}",
        "# TYPE pve_power_dc_load_watts gauge",
        f"pve_power_dc_load_watts {r['dc_load_w']}",
        "# TYPE pve_power_cpu_utilisation gauge",
        f"pve_power_cpu_utilisation {r['cpu_util']}",
        "# HELP pve_power_energy_kwh_total Estimated energy used since first run",
        "# TYPE pve_power_energy_kwh_total counter",
        f"pve_power_energy_kwh_total {r['energy_kwh']}",
        "# TYPE pve_power_component_watts gauge",
    ]
    out += [f'pve_power_component_watts{{component="{_esc(k)}"}} {v}' for k, v in r["components"].items()]
    out.append("# TYPE pve_power_guest_cpu_watts gauge")
    out += [f'pve_power_guest_cpu_watts{{vmid="{g["vmid"]}",type="{g["type"]}",name="{_esc(g["name"])}"}} {g["cpu_w"]}'
            for g in r["guests"]]
    return "\n".join(out) + "\n"


def serve(est, bind, port):
    latest = {"r": None}
    lock = threading.Lock()
    stop = threading.Event()

    def loop():
        while not stop.wait(INTERVAL_S):
            try:
                r = est.sample()
                with lock:
                    latest["r"] = r
            except Exception as e:
                print(f"sample failed: {e}", file=sys.stderr)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            with lock:
                r = latest["r"]
            if r is None:
                self.send_error(503, "warming up")
                return
            if self.path.startswith("/metrics"):
                body, ctype = render_prometheus(r).encode(), "text/plain; version=0.0.4"
            elif self.path in ("/", "/json"):
                body, ctype = json.dumps(r).encode(), "application/json"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer((bind, port), Handler)

    def shutdown(*_):
        stop.set()
        est.save_energy()
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    threading.Thread(target=loop, daemon=True).start()
    print(f"serving on http://{bind}:{port}/json and /metrics", file=sys.stderr)
    httpd.serve_forever()


def main():
    ap = argparse.ArgumentParser(description="Estimate Proxmox host wall power")
    ap.add_argument("--once", action="store_true", help="take one sample and exit")
    ap.add_argument("--json", action="store_true", help="print JSON instead of the table")
    ap.add_argument("--serve", type=int, metavar="PORT", help="run HTTP endpoint on PORT")
    ap.add_argument("--bind", default="0.0.0.0", help="address for --serve (default 0.0.0.0)")
    args = ap.parse_args()

    if os.geteuid() != 0:
        sys.exit("run as root: RAPL energy counters are only readable by root")

    est = SmcEstimator() if smc_sensors() else Estimator()

    if args.serve:
        serve(est, args.bind, args.serve)
        return

    try:
        while True:
            time.sleep(INTERVAL_S)
            r = est.sample()
            print(json.dumps(r, indent=2) if args.json else render_terminal(r), flush=True)
            if args.once:
                break
    except KeyboardInterrupt:
        pass
    finally:
        est.save_energy()


if __name__ == "__main__":
    main()
