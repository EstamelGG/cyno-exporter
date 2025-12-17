import os, subprocess


class Plugins:
    def __init__(self, *plugin):
        self.cwd = "./tools"
        self.exe = os.path.join(self.cwd, *plugin)

    def run(self, *args):
        stdout = subprocess.run(
            [self.exe, *args],
            creationflags=subprocess.CREATE_NO_WINDOW,
            check=False,
            capture_output=True,
            text=True,
        ).stdout
        return stdout


class Gr2ToJson(Plugins):
    def __init__(self):
        super().__init__("gr2tojson", "gr2tojson.exe")

    def run(self, *args):
        super().run(*args)


class NvttExport(Plugins):
    def __init__(self):
        super().__init__("dds2png", "nvtt_export.exe")

    def run(self, *args):
        filename = os.path.splitext(args[0])[0]
        dir_path = os.path.dirname(args[0])
        super().run(args[0], "-o", os.path.join(dir_path, f"{filename}.png"))
