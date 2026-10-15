import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

sdk = Path(os.environ["ANDROID_HOME"])
output = Path("out/pr11-stock-control")
output.mkdir(parents=True)
avd_home = Path(os.environ["RUNNER_TEMP"]) / "pr11-stock-avd"
avd_home.mkdir()
env = dict(os.environ, ANDROID_AVD_HOME=str(avd_home))
name = "pr11-stock-api25"
serial = "emulator-5556"
ramdisk = sdk / "system-images/android-25/default/x86/ramdisk.img"
before = hashlib.sha256(ramdisk.read_bytes()).hexdigest()

def adb(*args, check=True):
    result = subprocess.run([str(sdk / "platform-tools/adb"), "-s", serial, *args],
                            capture_output=True, text=True, timeout=30)
    if check and result.returncode:
        raise RuntimeError(result.stdout + result.stderr)
    return result.stdout.strip()

def ready():
    for _ in range(180):
        try:
            if adb("shell", "getprop", "sys.boot_completed", check=False) == "1":
                return
        except subprocess.TimeoutExpired:
            pass
        time.sleep(1)
    raise RuntimeError("Stock AVD did not boot")

subprocess.run([str(sdk / "cmdline-tools/latest/bin/avdmanager"), "create", "avd",
                "-n", name, "-k", "system-images;android-25;default;x86"],
               input="no\n", text=True, env=env, check=True)
engine_log = (output / "engine.log").open("w")
process = subprocess.Popen([str(sdk / "emulator/emulator"), "@" + name,
                            "-port", "5556", "-no-window", "-no-audio", "-no-boot-anim",
                            "-no-snapshot", "-read-only", "-wipe-data", "-cores", "2",
                            "-memory", "3072", "-gpu", "swiftshader_indirect",
                            "-feature", "-SystemAsRoot"],
                           stdout=engine_log, stderr=subprocess.STDOUT, env=env)
records = []
try:
    for index in range(4):
        ready()
        identity = {"boot_id": adb("shell", "cat", "/proc/sys/kernel/random/boot_id"),
                    "fingerprint": adb("shell", "getprop", "ro.build.fingerprint"),
                    "magisk_absent": adb("shell", "command -v magisk || echo absent") == "absent"}
        assert identity["magisk_absent"]
        time.sleep(10)
        adb("root", check=False)
        for _ in range(30):
            if adb("shell", "id", "-u", check=False) == "0":
                break
            time.sleep(1)
        state = adb("shell", "getprop", "init.svc.mediaextractor")
        if state == "running":
            for _ in range(8):
                adb("shell", "setprop", "ctl.restart", "mediaextractor")
                time.sleep(1)
        boot = adb("logcat", "-d", "-b", "all", "-v", "threadtime")
        crash = adb("logcat", "-d", "-b", "crash", "-v", "threadtime")
        (output / f"boot-{index}.log").write_text(boot)
        (output / f"crash-{index}.log").write_text(crash)
        identity["denied_nanosleep"] = "blocked syscall: nanosleep" in boot
        identity["minijail_crash"] = "log_sigsys_handler" in crash
        identity["service_after"] = adb("shell", "service", "check", "media.extractor")
        records.append(identity)
        print(json.dumps(identity), flush=True)
        if index != 3:
            adb("reboot")
            time.sleep(5)
finally:
    process.terminate()
    process.wait(timeout=30)
    engine_log.close()
    after = hashlib.sha256(ramdisk.read_bytes()).hexdigest()
    assert before == after
    (output / "record.json").write_text(json.dumps({"stock_only": True,
        "image": "system-images;android-25;default;x86", "ramdisk_sha256": before,
        "unchanged_ramdisk": True, "boots": records}, indent=2) + "\n")
