"""
Regression test for the WS90 `total_piezo` field.

GW2000A firmware V3.3.3 added a `total_piezo` field to the push body. The
exporter used to look up every *_piezo key in `rainmaps`, so this unknown key
raised a KeyError and every POST /report returned 500.

Starts `ecowitt_exporter.py` as a subprocess on a random free port, POSTs a
V3.3.3 push body with the WS90 and WH52 fields plus `total_piezo`
(`test_fixtures/total_piezo_post.txt`), and scrapes `/metrics` to verify that
the POST returns 200 and that the rain and WH52 metrics are exported. It then
POSTs a body with fields that cannot be processed, and checks that they are
skipped, logged and counted in ecowitt_skipped_fields_total without failing
the rest of the push. Finally it POSTs more distinct failing fields than
MAX_SKIPPED_FIELDS, and checks that the extra ones are counted as
field="other".

The exporter runs with the same interpreter as the test, so run it with one
that has `requirements.txt` installed, e.g.
`.venv/bin/python test_total_piezo.py`.
"""

import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from test_wh52 import _pick_port, _stderr_text, _wait_listening


REPO_ROOT = Path(__file__).resolve().parent
FIXTURE = REPO_ROOT / "test_fixtures" / "total_piezo_post.txt"

EXPECTED = [
    # total_piezo=31.453 (inches) converted to mm, labelled like the other
    # piezo keys
    'ecowitt_rain{sensor="total_piezo",unit="mm"} 798.9',
    'ecowitt_rain{sensor="yrain_piezo",unit="mm"} 639.3',
    'ecowitt_rain{sensor="mrain_piezo",unit="mm"} 17.9',
    'ecowitt_rain{sensor="last24hrain_piezo",unit="mm"} 1.0',
    'ecowitt_rain_state{sensor="srain_piezo"} 0.0',
    'ecowitt_wh90{sensor="ws90cap_volt",unit="volt"} 4.6',
    'ecowitt_soilmoisture{sensor="soil_ec_hum1",unit="percent"} 50.0',
    # soil_ec_temp1=62.78 (Fahrenheit) converted to Celsius
    'ecowitt_soiltemp{sensor="soil_ec_temp1",unit="c"} 17.1',
    'ecowitt_soilec{sensor="soil_ec1",unit="uscm"} 410.0',
    'ecowitt_batteryvoltage{sensor="soil_ec_batt1",unit="volt"} 1.6',
    'ecowitt_sensor_last_report_timestamp_seconds{sensor="soil_ec_hum1"}',
]

# Fields that make the handler raise (no TEMP9_LOCATION, unknown barometer),
# followed by a valid field that must still be exported
BAD_BODY = "temp9f=70.00&baromxin=30.000&tempf=68.00"
BAD_EXPECTED = 'ecowitt_temp{location="outdoor",sensor="outdoor",unit="c"} 20.0'
BAD_LOGGED = ["Skipping field temp9f", "Skipping field baromxin"]
# baromxin must be skipped, not exported under a label left over from temp9f
BAD_UNEXPECTED = 'ecowitt_barom{sensor="ch9"'
BAD_COUNTED = [
    'ecowitt_skipped_fields_total{field="temp9f"} 1.0',
    'ecowitt_skipped_fields_total{field="baromxin"} 1.0',
]

# More distinct failing fields than MAX_SKIPPED_FIELDS (100) in the exporter;
# each raises "unknown barometer field"
MANY_BAD_BODY = "&".join(f"barombad{i}in=30.000" for i in range(105))


def _post(port: int, body: str) -> int:
    """POST a body as application/x-www-form-urlencoded, as the gateway does."""
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/report",
        data=body.encode("ascii"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=3) as resp:
        return resp.status


def _metrics(port: int) -> str:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=3) as resp:
        return resp.read().decode("utf-8")


def main() -> int:
    """Run the exporter, POST the fixtures and check /metrics."""
    body = FIXTURE.read_text(encoding="ascii").strip()

    port = _pick_port()
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    env["TEMPERATURE_UNIT"] = "c"
    env["RAIN_UNIT"] = "mm"

    # The exporter hardcodes port 8088 in app.run(), so run a copy of the
    # source with that literal rewritten (see test_wh52.py).
    src = (REPO_ROOT / "ecowitt_exporter.py").read_text(encoding="utf-8")
    patched = src.replace("port=8088", f"port={port}")
    patched = patched.replace('host="0.0.0.0"', 'host="127.0.0.1"')
    if f"port={port}" not in patched:
        raise RuntimeError("failed to patch app.run port; source layout changed")

    errors = []
    with tempfile.TemporaryFile() as stderr_file, subprocess.Popen(
        [sys.executable, "-c", patched],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=stderr_file,
    ) as proc:
        try:
            _wait_listening(port, proc, stderr_file)

            try:
                status = _post(port, body)
            except urllib.error.HTTPError as exc:
                status = exc.code
            if status != 200:
                errors.append(f"POST of {FIXTURE.name} returned {status}")
            metrics_text = _metrics(port)

            try:
                bad_status = _post(port, BAD_BODY)
            except urllib.error.HTTPError as exc:
                bad_status = exc.code
            if bad_status != 200:
                errors.append(f"POST with unprocessable fields returned {bad_status}")
            bad_metrics_text = _metrics(port)

            try:
                many_status = _post(port, MANY_BAD_BODY)
            except urllib.error.HTTPError as exc:
                many_status = exc.code
            if many_status != 200:
                errors.append(f"POST with more distinct failing fields than the cap returned {many_status}")
            many_metrics_text = _metrics(port)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
        stderr_text = _stderr_text(stderr_file)

    errors += [f"MISSING: {m}" for m in EXPECTED if m not in metrics_text]
    if BAD_EXPECTED not in bad_metrics_text:
        errors.append(f"MISSING after unprocessable fields: {BAD_EXPECTED}")
    if BAD_UNEXPECTED in bad_metrics_text:
        errors.append(f"UNEXPECTED after unprocessable fields: {BAD_UNEXPECTED}")
    errors += [f"NOT LOGGED: {m}" for m in BAD_LOGGED if m not in stderr_text]
    errors += [f"NOT COUNTED: {m}" for m in BAD_COUNTED if m not in bad_metrics_text]

    # temp9f and baromxin, then the first 98 barombad fields get their own
    # label; the last 7 are counted as field="other"
    named = [l for l in many_metrics_text.splitlines()
             if l.startswith("ecowitt_skipped_fields_total{")
             and 'field="other"' not in l]
    if len(named) != 100:
        errors.append(f"expected 100 named skipped-field labels, got {len(named)}")
    if 'ecowitt_skipped_fields_total{field="other"} 7.0' not in many_metrics_text:
        errors.append('MISSING: ecowitt_skipped_fields_total{field="other"} 7.0')

    if errors:
        for e in errors:
            print(e)
        print("\n--- /metrics (rain, soil and temp lines) ---")
        for line in bad_metrics_text.splitlines():
            if not line.startswith("#") and any(s in line for s in ("rain", "soil", "temp")):
                print(line)
        print(f"\n--- exporter stderr ---\n{stderr_text}")
        return 1

    print(f"OK - POSTs returned 200, all {len(EXPECTED)} metrics present, "
          "unprocessable fields skipped, logged and counted (capped)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
