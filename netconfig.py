"""Read config.ini (capacity, delay, loss, background traffic, test settings)."""
import configparser
import os

DEFAULT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.ini")


def load(path=None):
    path = path or DEFAULT_FILE
    cp = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
    if not cp.read(path):
        raise SystemExit(f"Config file not found: {path}")

    def path_cfg(sec):
        return {"bw": cp.getfloat(sec, "capacity_mbps"),
                "delay_ms": cp.getfloat(sec, "delay_ms"),
                "loss": cp.getfloat(sec, "loss_pct")}

    return {
        "file": path,
        "wifi": dict(path_cfg("wifi"),
                     distance_m=cp.getfloat("wifi", "distance_m", fallback=5)),
        "5g": path_cfg("5g"),
        "bg_share": cp.getfloat("background", "share"),
        "bg_min": cp.getfloat("background", "min_share"),
        "bg_max": cp.getfloat("background", "max_share"),
        "bg_step": cp.getint("background", "step_s"),
        "duration": cp.getint("test", "duration_s"),
        "seed": cp.getint("test", "seed"),
    }


def summary(cfg):
    w, f = cfg["wifi"], cfg["5g"]
    return (f"config: {cfg['file']}\n"
            f"  Wi-Fi: {w['bw']:g} Mbit/s, delay {w['delay_ms']:g} ms, loss {w['loss']:g} %, "
            f"station {w.get('distance_m', 5):g} m from AP\n"
            f"  5G   : {f['bw']:g} Mbit/s, delay {f['delay_ms']:g} ms, loss {f['loss']:g} %\n")
