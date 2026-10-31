import shutil
import subprocess
import psutil
from diagnostic_msgs.msg import DiagnosticStatus
from so101_core.config import resolve
from so101_core.ros_utils import SystemNode, run


class DiagnosticsNode(SystemNode):
    def __init__(self):
        super().__init__("system_diagnostics")
        self.create_timer(2.0, self.tick)

    def tick(self):
        root = resolve(self.config["dataset"], self.config["dataset"]["bag_root"])
        root.mkdir(parents=True, exist_ok=True)
        disk, memory = shutil.disk_usage(root), psutil.virtual_memory()
        values = {"cpu_percent": psutil.cpu_percent(), "ram_percent": memory.percent,
                  "disk_free_gb": round(disk.free/1e9, 2), "disk_used_percent": 100*disk.used/disk.total}
        if hasattr(psutil, "sensors_temperatures"):
            values["cpu_temperatures"] = psutil.sensors_temperatures()
        try:
            gpu = subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu",
                                  "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=0.5)
            values["gpu"] = gpu.stdout.strip() if gpu.returncode == 0 else "unavailable"
        except (OSError, subprocess.TimeoutExpired):
            values["gpu"] = "unavailable"
        low = disk.free < self.config["safety"]["min_disk_free_gb"]*1e9
        self.diagnostic(DiagnosticStatus.ERROR if low else DiagnosticStatus.OK,
                        "Low disk capacity" if low else "System resources", **values)


def main():
    run(DiagnosticsNode)
