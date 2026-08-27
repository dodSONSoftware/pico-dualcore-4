import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]

CORE0_FILES = ("core0.py", "wifi.py", "mqtt.py", "mqtt_client.py")
CORE1_FILES = (
    "core1.py",
    "device_manager.py",
    "device_factory.py",
    "system_information.py",
    "devices/device.py",
    "devices/system_information/system_information_device.py",
)


def _imports(path):
    tree = ast.parse((ROOT / path).read_text())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                names.add(item.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_core1_has_no_network_stack_imports():
    forbidden = {"network", "socket", "mqtt", "mqtt_client", "wifi", "core0", "led_manager"}
    for path in CORE1_FILES:
        assert not (_imports(path) & forbidden), path


def test_core0_has_no_sensor_stack_imports():
    forbidden = {"core1", "device_manager", "device_factory", "devices", "system_information"}
    for path in CORE0_FILES:
        assert not (_imports(path) & forbidden), path


def test_core1_has_no_mqtt_topic_configuration():
    for path in CORE1_FILES:
        source = (ROOT / path).read_text()
        assert "mqtt_topic_" not in source, path
