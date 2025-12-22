from datetime import datetime
from pathlib import Path
import sys, os, time, shutil, json, concurrent.futures, argparse, zipfile, re, hashlib
import requests
import yaml
from dotenv import load_dotenv
from PyQt6.QtWidgets import (
    QApplication,
    QMainWindow,
    QTreeWidget,
    QTreeWidgetItem,
    QMenu,
    QFileDialog,
    QProgressDialog,
    QProgressBar,
    QWidget,
    QDialog,
    QLineEdit,
    QVBoxLayout,
    QTextEdit,
    QHeaderView,
    QLabel,
    QMenuBar,
    QSizePolicy,
    QMessageBox,
    QPushButton,
    QTabWidget,
)
from PyQt6.QtGui import QIcon, QPixmap, QAction, QKeySequence, QShortcut, QBrush, QColor
from PyQt6.QtCore import Qt, QObject, pyqtSignal, QThread
from utils.obj import Wavefront
from utils.plugins import NvttExport

load_dotenv()

CONFIG_FILE = "./config.json"
VERSION = "v1.8.1"
WINDOW_TITLE = f"Cyno Exporter {VERSION}"
CLIENTS = {
    "sharedCache": {"name": "Local", "id": None},
}
STYLE_SHEET = open(
    os.path.join(Path(__file__).parent, "style.qss"), "r", encoding="utf-8"
).read()
DB = json.loads(open("./db.json", "r").read())


class EVEDirectory(QTreeWidgetItem):
    def __init__(self, parent, text="", icon=None):
        super().__init__(parent)
        self.setText(0, text)
        self.setIcon(0, icon)
        self.items = []
        self.size = int()

    def add(self, item):
        self.items.append(item)


class EVEFile(QTreeWidgetItem):
    def __init__(
        self,
        parent,
        text="",
        filename="",
        respath="",
        resfile_hash="",
        size=0,
        icon=QIcon(),
    ):
        super().__init__(parent)
        self.setText(0, text)
        self.setIcon(0, icon)
        self.filename = filename
        self.size = int(size)
        self.respath = respath
        self.resfile_hash = resfile_hash
        self.setToolTip(0, filename)


class ResFileIndex:
    def __init__(self, chinese_client=False, event_logger=None):
        self.chinese_client = chinese_client
        self.event_logger = event_logger

        if not chinese_client:
            self.binaries_url = "https://binaries.eveonline.com"
            self.resources_url = "https://resources.eveonline.com"
        else:
            self.chinese_url = os.environ.get("CHINESE_RESINDEX_CDN")
            self.binaries_url = f"{os.environ.get('CHINESE_CDN')}/binaries"
            self.resources_url = f"{os.environ.get('CHINESE_CDN')}/resources"

    def fetch_client(self, client, timeout=10):
        base_url = self.chinese_url if self.chinese_client else self.binaries_url
        url = f"{base_url}/{client}"
        try:
            response = requests.get(url, timeout=timeout)
            self.event_logger.add(f"Requesting client: {url} | Response: {response.status_code}")
            if response.status_code == 200:
                client = response.json()
                if not self._is_protected(client):
                    return self._get_build(client)
                else:
                    return None
        except requests.exceptions.MissingSchema:
            self.event_logger.add(f"Connection failed: {url}")
        except Exception as e:
            self.event_logger.add(f"Connection failed to: {url} | Error: {str(e)}")

    @staticmethod
    def resindexfile_object(content):
        resfile_list = []
        for line in sorted(filter(bool, content.lstrip().splitlines())):
            data = line.lower().split(",")
            resfile_list.append(
                {
                    "res_path": data[0].split(":/")[1],
                    "resfile_hash": data[1],
                    "size": data[3],
                }
            )

        return resfile_list


    def fetch_resindexfile(self, build):
        base_url = self.chinese_url if self.chinese_client else self.binaries_url
        url = f"{base_url}/eveonline_{build}.txt"
        response = requests.get(url)
        self.event_logger.add(f"Requesting resindex: {url} | Response: {response.status_code}")
        if response.status_code == 200:
            resfileindex = next(
                (
                    resfile
                    for resfile in ResFileIndex.resindexfile_object(response.text)
                    if resfile["res_path"].startswith("resfileindex.txt")
                ),
                None,
            )

            resfileindex_file = f"{build}_resfileindex.txt"

            os.makedirs("resindex", exist_ok=True)
            resfileindex_file_path = os.path.join("resindex", resfileindex_file)
            download_url = f"{self.binaries_url}/{resfileindex['resfile_hash']}"
            download_response = requests.get(download_url)
            self.event_logger.add(f"Downloading resfileindex: {download_url} | Response: {download_response.status_code}")
            if download_response.status_code == 200:
                with open(resfileindex_file_path, "wb") as f:
                    f.write(download_response.content)
                return resfileindex_file
            else:
                self.event_logger.add(f"Failed to download resfileindex: {download_url} | Response: {download_response.status_code}")
        return None

    def _is_protected(self, client):
        return bool(client["protected"])

    def _get_build(self, client):
        return int(client["build"])


class ResTree(QTreeWidget):
    def __init__(
        self,
        parent=None,
        client=None,
        chinese_client=False,
        event_logger=None,
        shared_cache=None,
        ship_tree=None,
    ):
        super().__init__(parent)

        self.setHeaderLabel("res: ► ")
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self.show_context_menu)
        self.itemSelectionChanged.connect(self._show_selected_item)
        self.setHeaderLabels(["", "Size"])

        self.setColumnWidth(0, 775)
        self.setColumnWidth(1, 50)
        self.header().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self.setIndentation(15)  # 设置缩进为15像素

        self.chinese_client = chinese_client
        self.client = client

        self.shared_cache = shared_cache
        self.are_resfiles_loaded = False
        self.event_logger = event_logger
        self.ship_tree = ship_tree  # 保存 ShipTree 引用，用于同步启动 SDE 下载
        self.on_load_complete_callback = None  # 加载完成后的回调函数
        self.resfiles_list = []  # 保存资源文件列表，供 ShipTree 查找模型文件

        self.protected_label = None

        self.icon_atlas = QPixmap("./icons/icons.png")
        try:
            self.config = json.loads(open(CONFIG_FILE, "r", encoding="utf-8").read())
        except:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                f.write(json.dumps({"SharedCacheLocation": ""}, indent=4))

            self.config = json.loads(open(CONFIG_FILE, "r", encoding="utf-8").read())

        self.show()

    def _show_selected_item(self):
        try:
            print(f"Selected item: {self.selectedItems()[0].respath}")
            self.setHeaderLabel(
                "res: ► " + self.selectedItems()[0].respath.replace("/", " ► ")
            )
        except:
            pass

    def _get_path_segments(self, item):
        path_segments = []
        try:
            while item and item.text(0) != "res:":
                path_segments.insert(0, item.text(0))
                item = item.parent()
            return os.path.join(*path_segments)
        except:
            return ""

    def _get_directory_size(self, directory):
        total = 0
        for child in directory.items:
            if isinstance(child, EVEFile):
                total += int(child.size)
            elif isinstance(child, EVEDirectory):
                total += int(self._get_directory_size(child))
        directory.size = total
        return total

    def copy_folder_files(self, folder_item, base_path):
        files = []
        for i in range(folder_item.childCount()):
            child = folder_item.child(i)
            child_name = child.text(0)
            child_path = os.path.join(base_path, child_name)

            if child.childCount() > 0:
                files.extend(self.copy_folder_files(child, child_path))
            else:
                files.append(child)

        return files

    def download_file_itemless(self, resfile_hash, dest_path):
        resindex = ResFileIndex(
            chinese_client=self.chinese_client, event_logger=self.event_logger
        )
        try:
            url = f"{resindex.resources_url}/{resfile_hash}"
            response = requests.get(url, timeout=10)
            self.event_logger.add(f"Downloading file: {url} | Response: {response.status_code}")
            if response.status_code == 200:
                with open(dest_path, "wb") as f:
                    f.write(response.content)
                self.event_logger.add(f"{url} -> {dest_path}")
            elif response.status_code == 404:
                self.event_logger.add(f"404 error: {url}")
                return
        except Exception as e:
            self.event_logger.add(f"Request failed: {url} | Error: {str(e)}")

    def download_file(self, item, dest_path, retries=0):
        resindex = ResFileIndex(
            chinese_client=self.chinese_client, event_logger=self.event_logger
        )
        try:
            url = f"{resindex.resources_url}/{item.resfile_hash}"
            response = requests.get(url)
            self.event_logger.add(f"Downloading file: {url} | Response: {response.status_code} | File: {item.filename}")
            if response.status_code == 200:
                with open(dest_path, "wb") as f:
                    f.write(response.content)
                self.event_logger.add(f"{url} -> {dest_path}")
                if os.path.getsize(dest_path) != item.size:
                    self.event_logger.add(
                        f"resfile size doesn't match: {dest_path}, re-trying..."
                    )
                    if retries < 3:
                        self.download_file(item, dest_path, retries + 1)
            elif response.status_code == 404:
                self.event_logger.add(f"404 error: {url} | File: {item.filename}")
                return
            return item.filename
        except Exception as e:
            self.event_logger.add(f"Request failed: {url} | Error: {str(e)}")

    def _save_as_obj_command(self, item):
        out_file = self._save_file_command(item)
        if not out_file:
            return
        Wavefront().to_obj(out_file)
        self.event_logger.add(f"Obj exported: {out_file}")

    def _save_as_png(self, out_file_path):
        NvttExport().run(out_file_path)
        os.remove(out_file_path)


    def _save_file_command(self, item, multiple=False, multiple_destination=None, convert_dds=False):
        if not multiple:
            dest_location, _ = QFileDialog.getSaveFileName(
                None, "Save File", item.text(0), "All Files(*.)"
            )
            if not dest_location:
                return
            out_file_path = dest_location
        else:
            out_file_path = multiple_destination

        if self.client is None:
            folder, resfile_hash = item.resfile_hash.split("/", 1)
            source_path = os.path.join(
                self.config["SharedCacheLocation"], "ResFiles", folder, resfile_hash
            )
            shutil.copy(source_path, out_file_path)
            self.event_logger.add(f"{source_path} -> {out_file_path}")
        else:
            self.download_file(
                item=item,
                dest_path=out_file_path,
            )

        if convert_dds and out_file_path.lower().endswith(".dds"):
            self._save_as_png(out_file_path)
        if not multiple:
            self.event_logger.add(f"Exported resfile to: {out_file_path}")
        return out_file_path if not multiple else item.filename

    def _save_folder_command(self, item, convert_dds=False):
        dest_folder = QFileDialog.getExistingDirectory(None, "Select Destination")
        if not dest_folder:
            return

        path_segments = self._get_path_segments(item)
        files = self.copy_folder_files(item, path_segments)

        loading = LoadingScreenWindow(files, stay_on_top=True)

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as worker:
            futures = []

            for i, file in enumerate(files):
                if isinstance(file, EVEFile):
                    file_path = os.path.normpath(
                        os.path.join(dest_folder, file.respath)
                    )
                    os.makedirs(os.path.dirname(file_path), exist_ok=True)
                    futures.append(
                        worker.submit(self._save_file_command, file, True, file_path, convert_dds)
                    )

            for future in concurrent.futures.as_completed(futures):
                loading.label.setText(future.result())
                loading.setValue(loading.value() + 1)
                QApplication.processEvents()

        self.event_logger.add(f"Exported {len(files)} resfiles to {dest_folder}")
        loading.close()

    def _find_resfileindex_path(self, shared_cache_location):
        """
        智能查找 resfileindex.txt 文件路径，支持 Windows/Linux 和 macOS 的不同路径结构
        
        参数:
        - shared_cache_location: SharedCache 的根目录路径
        
        返回:
        - str: resfileindex.txt 的完整路径，如果未找到返回 None
        """
        if not shared_cache_location:
            return None
        
        # 尝试的路径列表（按优先级排序）
        possible_paths = [
            # Windows/Linux 路径
            os.path.join(shared_cache_location, "tq", "resfileindex.txt"),
            # macOS 路径
            os.path.join(shared_cache_location, "tq", "EVE.app", "Contents", "Resources", "build", "resfileindex.txt"),
        ]
        
        # 尝试每个路径
        for path in possible_paths:
            if os.path.exists(path) and os.path.isfile(path):
                self.event_logger.add(f"Found resfileindex.txt at: {path}")
                return path
        
        # 如果都没找到，记录所有尝试的路径
        self.event_logger.add(f"resfileindex.txt not found. Tried paths:")
        for path in possible_paths:
            self.event_logger.add(f"  - {path}")
        
        return None

    def _start_loading(self, root, resfileindex_path):
        with open(resfileindex_path, "r", encoding="utf-8") as f:
            resfileindex = ResFileIndex.resindexfile_object(f.read())

        # 保存资源文件列表，供 ShipTree 查找模型文件
        self.resfiles_list = resfileindex

        start_time = time.time()

        self.event_logger.add("Loading resfiles...")
        self._load_file_tree(root=root, resfiles=resfileindex)
        self.event_logger.add(f"Took {time.time() - start_time:.2f}s to load resfiles")

    def load_resfiles(self, parent, client=None):
        try:
            self.shared_cache.setEnabled(False)
            if self.are_resfiles_loaded:
                return
            parent.clear()
            root = EVEDirectory(parent, "res:", QIcon("./icons/res.png"))
            root.setExpanded(True)

            if self.protected_label is not None:
                self.protected_label.close()

            self.protected_label = QLabel("Client is protected", self)

            os.makedirs("resindex", exist_ok=True)

            # 同步启动 SDE 下载（如果 ShipTree 可用）
            if self.ship_tree and not self.ship_tree.are_ships_loaded:
                if not (self.ship_tree.download_thread and self.ship_tree.download_thread.isRunning()):
                    self.event_logger.add("Starting SDE download in background while loading Local files...")
                    # 调用 ShipTree 的启动下载方法
                    self.ship_tree._start_sde_download()

            self.config = json.loads(open(CONFIG_FILE, "r").read())
            try:
                shared_cache_location = self.config.get("SharedCacheLocation", "")
                if not shared_cache_location:
                    raise ValueError("SharedCacheLocation is not configured in config.json")
                
                # 使用智能查找方法定位 resfileindex.txt
                resfileindex_path = self._find_resfileindex_path(shared_cache_location)
                if not resfileindex_path:
                    raise FileNotFoundError(
                        f"resfileindex.txt not found in SharedCache location: {shared_cache_location}\n"
                        f"Please check if the path is correct and the file exists."
                    )
                
                self._start_loading(root, resfileindex_path)
            except OSError as e:
                error_msg = f"Invalid Shared Cache location. Check config.json\n错误详情: {str(e)}"
                print(f"错误发生在 load_resfiles (OSError): {error_msg}", file=sys.stderr, flush=True)
                QMessageBox.warning(
                    self, "Error", error_msg
                )
            except Exception as e:
                error_msg = f"加载 resfiles 时发生错误: {str(e)}"
                print(f"错误发生在 load_resfiles: {error_msg}", file=sys.stderr, flush=True)
                import traceback
                traceback.print_exc(file=sys.stderr)
                QMessageBox.critical(
                    self, "Error", error_msg
                )
                raise

            self.shared_cache.setEnabled(True)
        except Exception as e:
            print(f"错误发生在 load_resfiles (外层): {e}", file=sys.stderr, flush=True)
            import traceback
            traceback.print_exc(file=sys.stderr)
            self.shared_cache.setEnabled(True)  # 确保重新启用控件
            raise

    def add_directory(self, part, parent, path, dir_map):
        if path not in dir_map:
            dir_item = EVEDirectory(
                parent, text=part, icon=QIcon(self.icon_atlas.copy(16, 0, 15, 16))
            )
            dir_map[path] = dir_item
            parent.add(dir_item)
            parent.setText(1, self._format_filesize(self._get_directory_size(parent)))
        return dir_map[path]

    def add_resfile_filter(self, i, name):
        if "_lowdetail" in name or "_mediumdetail" in name:
            i += 1
            return True
        return False

    def _load_file_tree(self, root, resfiles):
        dir_map = {}

        loading = ProgressBar(resfiles, self)
        # loading_label = QLabel("Building tree...", self)
        # loading_label.setGeometry(5, 770, 900, 15)
        # loading_label.setStyleSheet("font-weight: bold;")
        # loading_label.show()

        for i, resfile in enumerate(resfiles):
            path_segments = resfile["res_path"].split("/")
            parent = root
            full_path = ""

            file_name = path_segments[-1]
            ext = os.path.splitext(file_name)
            icon = self.set_icon_from_extension(ext[1])

            # filter junk
            if self.add_resfile_filter(i, file_name):
                continue

            for segment in path_segments[:-1]:
                full_path = os.path.join(full_path, segment)
                parent = self.add_directory(segment, parent, full_path, dir_map)

            description = DB.get(file_name, file_name)
            file_item = EVEFile(
                parent,
                text=file_name,
                filename=description,
                size=resfile["size"],
                respath=resfile["res_path"],
                resfile_hash=resfile["resfile_hash"],
                icon=icon,
            )
            file_size = int(resfile["size"])
            file_item.setText(1, self._format_filesize(file_size))
            parent.add(file_item)

            current = parent
            while current:
                current.size = int(current.size) + file_size
                current.setText(1, self._format_filesize(current.size))
                current = current.parent()

            loading.setValue(i + 1)
            QApplication.processEvents()

        loading.close()
        # loading_label.close()
        self.are_resfiles_loaded = True
        
        # Local 加载完成后，通过回调函数启用 Ship 标签页
        if self.on_load_complete_callback:
            self.on_load_complete_callback()
        self.event_logger.add("Local files loaded. Ship tab is now available.")

    def set_icon_from_extension(self, ext):
        if ext == ".png":
            return QIcon(self.icon_atlas.copy(97, 0, 15, 16))
        elif ext == ".dds":
            return QIcon(self.icon_atlas.copy(33, 0, 15, 16))
        elif ext == ".jpg":
            return QIcon(self.icon_atlas.copy(81, 0, 15, 16))
        elif ext == ".gr2":
            return QIcon(self.icon_atlas.copy(177, 0, 15, 16))
        elif ext in (".txt", ".yaml", ".xml", ".json"):
            return QIcon(self.icon_atlas.copy(130, 0, 15, 16))
        elif ext == ".webm":
            return QIcon(self.icon_atlas.copy(65, 0, 15, 16))
        else:
            return QIcon(self.icon_atlas.copy(161, 0, 15, 16))

    def _format_filesize(self, size):
        size = float(size)
        for unit in ["KB", "MB", "GB"]:
            size /= 1024
            if size <= 1024:
                return f"{size:.2f} {unit}"

    def show_context_menu(self, point):
        item = self.itemAt(point)
        if item:
            menu = QMenu(self)

            if isinstance(item, EVEDirectory) and item.text(0) != "res:":
                save_folder_action = menu.addAction("Save folder" )
                save_folder_action.triggered.connect(lambda: self._save_folder_command(item))
                menu.addSeparator()
                save_folder_and_convert_dds_action = menu.addAction("Save folder | convert dds -> png")
                save_folder_and_convert_dds_action.triggered.connect(lambda: self._save_folder_command(item, convert_dds=True))
            elif isinstance(item, EVEFile):
                sub_menu = QMenu("Export...", menu)
                sub_menu.installEventFilter(ContextMenuFilter(sub_menu))
                menu.addMenu(sub_menu)
                save_file_action = sub_menu.addAction("Save file")
                save_file_action.triggered.connect(lambda: self._save_file_command(item))
                sub_menu.addSeparator()
                if item.text(0).endswith(".gr2"):
                    sub_menu.addSeparator()
                    export_obj_action = sub_menu.addAction("Save as .obj")
                    export_obj_action.triggered.connect(lambda: self._save_as_obj_command(item))
                elif item.text(0).endswith(".dds"):
                    sub_menu.addSeparator()
                    export_png_action = sub_menu.addAction("Save as .png")
                    export_png_action.triggered.connect(lambda: self._save_as_png(self._save_file_command(item)))

                menu.addAction(f"{item.filename}").setEnabled(False)

            menu.installEventFilter(ContextMenuFilter(menu))
            menu.popup(self.viewport().mapToGlobal(point))


class ResFileDependenciesDownloadThread(QThread):
    """后台下载 resfiledependencies.yaml 的线程"""
    download_complete = pyqtSignal(bool, int, str, dict)  # success, build_number, cache_file, dependencies
    
    def __init__(self, build_number, cache_file, event_logger):
        super().__init__()
        self.build_number = build_number
        self.cache_file = cache_file
        self.event_logger = event_logger
    
    def run(self):
        """在后台线程中执行下载"""
        try:
            self.event_logger.add("Fetching resfiledependencies.yaml...")
            file_urls = get_file_urls(self.build_number, ["resfiledependencies.yaml"], event_logger=self.event_logger)
            
            if "resfiledependencies.yaml" not in file_urls:
                self.event_logger.add("Failed to find resfiledependencies.yaml URL")
                self.download_complete.emit(False, self.build_number, self.cache_file, {})
                return
            
            url = file_urls["resfiledependencies.yaml"]
            self.event_logger.add(f"Downloading resfiledependencies.yaml: {url}")
            response = requests.get(url, timeout=30)
            self.event_logger.add(f"Response: {response.status_code}")
            response.raise_for_status()
            
            # 解析 YAML
            dependencies = yaml.safe_load(response.text) or {}
            
            # 保存到缓存
            with open(self.cache_file, 'w', encoding='utf-8') as f:
                yaml.dump(dependencies, f, allow_unicode=True)
            
            self.event_logger.add(f"Downloaded and cached {len(dependencies)} dependencies")
            self.download_complete.emit(True, self.build_number, self.cache_file, dependencies)
        except Exception as e:
            self.event_logger.add(f"Error downloading resfiledependencies.yaml: {str(e)}")
            self.download_complete.emit(False, self.build_number, self.cache_file, {})


class ContextMenuFilter(QObject):
    def eventFilter(self, context_menu, event):
        if isinstance(context_menu, QMenu):
            if event.type() == event.Type.MouseButtonPress:
                if event.button() == Qt.MouseButton.RightButton:
                    context_menu.close()
                    return True
        return False


def get_file_urls(build_number, file_names, event_logger=None):
    """
    从 eveonline_{build_number}.txt 获取文件下载 URL
    
    参数:
    - build_number: 构建号
    - file_names: 要查找的文件名列表
    - event_logger: 事件日志记录器（可选）
    
    返回:
    - dict: {file_name: full_url}
    """
    url = f"https://binaries.eveonline.com/eveonline_{build_number}.txt"
    try:
        response = requests.get(url, timeout=10)
        log_msg = f"Requesting file list: {url} | Response: {response.status_code}"
        if event_logger:
            event_logger.add(log_msg)
        else:
            print(log_msg)
        response.raise_for_status()
        content = response.text
        file_urls = {}
        
        for line in content.splitlines():
            for file_name in file_names:
                if f'{file_name},' in line:
                    parts = line.split(',')
                    if len(parts) >= 2:
                        url_part = parts[1]
                        full_url = f"https://binaries.eveonline.com/{url_part}"
                        file_urls[file_name] = full_url
                        print(f"Found {file_name} URL: {full_url}")
                        break
        
        missing_files = [f for f in file_names if f not in file_urls]
        if missing_files:
            raise ValueError(f"Failed to find download links for the following files: {', '.join(missing_files)}")
        
        return file_urls
    except requests.exceptions.HTTPError as e:
        error_msg = f"HTTP error getting file list: {e.response.status_code} - {e.response.reason} | URL: {url}"
        if event_logger:
            event_logger.add(error_msg)
        else:
            print(error_msg, file=sys.stderr)
        sys.exit(1)
    except requests.RequestException as e:
        error_msg = f"Request error getting file list: {str(e)} | URL: {url}"
        if event_logger:
            event_logger.add(error_msg)
        else:
            print(error_msg, file=sys.stderr)
        sys.exit(1)


class SDEDownloader:
    def __init__(self, event_logger=None):
        self.event_logger = event_logger
        self.latest_url = "https://developers.eveonline.com/static-data/tranquility/latest.jsonl"
        self.sde_base_url = "https://developers.eveonline.com/static-data/tranquility"
        self.cache_dir = "./sde_cache"
        os.makedirs(self.cache_dir, exist_ok=True)

    def _log(self, message):
        if self.event_logger:
            self.event_logger.add(message)

    def get_latest_build(self):
        """获取最新的 SDE build number"""
        try:
            self._log(f"Fetching latest SDE build number from: {self.latest_url}")
            response = requests.get(self.latest_url, timeout=10)
            self._log(f"Response: {response.status_code}")
            if response.status_code == 200:
                # 解析 JSONL 格式（每行一个 JSON 对象）
                for line in response.text.strip().splitlines():
                    if line:
                        try:
                            data = json.loads(line)
                            # 检查是否是 SDE 数据：_key 为 "sde" 或直接包含 buildNumber
                            if data.get("_key") == "sde" and "buildNumber" in data:
                                build_number = data["buildNumber"]
                                release_date = data.get("releaseDate", "Unknown")
                                self._log(f"Latest SDE build number: {build_number} (Release: {release_date})")
                                return build_number
                        except json.JSONDecodeError as e:
                            self._log(f"Failed to parse JSON line: {str(e)}")
                            continue
                self._log("Warning: No SDE build number found in latest.jsonl response")
                self._log(f"Response content: {response.text[:200]}")
                return None
            else:
                error_msg = f"Failed to fetch latest build: HTTP {response.status_code} - {response.reason}"
                self._log(error_msg)
                return None
        except requests.exceptions.Timeout:
            error_msg = f"Timeout while fetching latest build from {self.latest_url}"
            self._log(error_msg)
            return None
        except requests.exceptions.ConnectionError as e:
            error_msg = f"Connection error while fetching latest build: {str(e)}"
            self._log(error_msg)
            return None
        except requests.exceptions.RequestException as e:
            error_msg = f"Request error while fetching latest build: {str(e)}"
            self._log(error_msg)
            return None
        except Exception as e:
            import traceback
            error_msg = f"Unexpected error fetching latest build: {str(e)}\n{traceback.format_exc()}"
            self._log(error_msg)
            return None

    def get_sde_url(self, build_number, variant="jsonl"):
        """构建 SDE 下载 URL"""
        filename = f"eve-online-static-data-{build_number}-{variant}.zip"
        return f"{self.sde_base_url}/{filename}"

    def is_downloaded(self, build_number, variant="jsonl"):
        """检查 SDE 是否已下载并解压，同时验证 ZIP 文件完整性"""
        zip_path = os.path.join(self.cache_dir, f"eve-online-static-data-{build_number}-{variant}.zip")
        extract_dir = os.path.join(self.cache_dir, f"eve-online-static-data-{build_number}-{variant}")
        
        # 检查 zip 文件是否存在
        if not os.path.exists(zip_path):
            return False
        
        # 检查 ZIP 文件是否有效且完整
        try:
            # 检查是否是有效的 ZIP 文件
            if not zipfile.is_zipfile(zip_path):
                self._log(f"Invalid ZIP file detected: {zip_path}")
                return False
            
            # 尝试打开并测试 ZIP 文件完整性
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                # 测试 ZIP 文件的完整性（这会检查 CRC 和文件结构）
                bad_file = zip_ref.testzip()
                if bad_file is not None:
                    self._log(f"Corrupted file detected in ZIP: {bad_file} in {zip_path}")
                    return False
        except zipfile.BadZipFile:
            self._log(f"Bad ZIP file detected: {zip_path}")
            return False
        except Exception as e:
            self._log(f"Error checking ZIP file integrity: {str(e)}")
            return False
        
        # 检查解压目录是否存在且有内容
        if os.path.exists(extract_dir):
            try:
                if os.listdir(extract_dir):
                    return True
            except OSError:
                # 如果无法读取目录，认为不存在
                return False
        
        return False

    def download_sde(self, build_number, variant="jsonl", progress_callback=None):
        """下载 SDE 数据"""
        url = self.get_sde_url(build_number, variant)
        zip_path = os.path.join(self.cache_dir, f"eve-online-static-data-{build_number}-{variant}.zip")
        
        try:
            self._log(f"Downloading SDE from: {url}")
            self._log(f"Save to: {zip_path}")
            response = requests.get(url, stream=True, timeout=60)
            self._log(f"Response: {response.status_code}")
            response.raise_for_status()
            
            total_size = int(response.headers.get('content-length', 0))
            if total_size > 0:
                self._log(f"File size: {self._format_size(total_size)}")
            downloaded = 0
            
            with open(zip_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        if progress_callback and total_size > 0:
                            progress = int((downloaded / total_size) * 100)
                            progress_callback(progress)
            
            # 验证下载的文件大小
            actual_size = os.path.getsize(zip_path)
            if total_size > 0 and actual_size != total_size:
                error_msg = f"Download incomplete: expected {total_size} bytes, got {actual_size} bytes"
                self._log(error_msg)
                if os.path.exists(zip_path):
                    os.remove(zip_path)
                return None
            
            self._log(f"SDE downloaded successfully: {zip_path} ({self._format_size(actual_size)})")
            return zip_path
        except requests.exceptions.HTTPError as e:
            error_msg = f"HTTP error downloading SDE: {e.response.status_code} - {e.response.reason}\nURL: {url}"
            self._log(error_msg)
            if os.path.exists(zip_path):
                try:
                    os.remove(zip_path)
                except:
                    pass
            return None
        except requests.exceptions.Timeout:
            error_msg = f"Timeout while downloading SDE from {url}"
            self._log(error_msg)
            if os.path.exists(zip_path):
                try:
                    os.remove(zip_path)
                except:
                    pass
            return None
        except requests.exceptions.ConnectionError as e:
            error_msg = f"Connection error while downloading SDE: {str(e)}\nURL: {url}"
            self._log(error_msg)
            if os.path.exists(zip_path):
                try:
                    os.remove(zip_path)
                except:
                    pass
            return None
        except requests.exceptions.RequestException as e:
            error_msg = f"Request error while downloading SDE: {str(e)}\nURL: {url}"
            self._log(error_msg)
            if os.path.exists(zip_path):
                try:
                    os.remove(zip_path)
                except:
                    pass
            return None
        except IOError as e:
            error_msg = f"IO error while saving SDE file: {str(e)}\nPath: {zip_path}"
            self._log(error_msg)
            if os.path.exists(zip_path):
                try:
                    os.remove(zip_path)
                except:
                    pass
            return None
        except Exception as e:
            import traceback
            error_msg = f"Unexpected error downloading SDE: {str(e)}\n{traceback.format_exc()}"
            self._log(error_msg)
            if os.path.exists(zip_path):
                try:
                    os.remove(zip_path)
                except:
                    pass
            return None

    def _format_size(self, size):
        """格式化文件大小"""
        for unit in ['B', 'KB', 'MB', 'GB']:
            if size < 1024.0:
                return f"{size:.2f} {unit}"
            size /= 1024.0
        return f"{size:.2f} TB"

    def extract_sde(self, zip_path, progress_callback=None):
        """解压 SDE 数据"""
        extract_dir = os.path.splitext(zip_path)[0]
        
        try:
            # 检查 zip 文件是否存在
            if not os.path.exists(zip_path):
                error_msg = f"ZIP file not found: {zip_path}"
                self._log(error_msg)
                return None
            
            # 检查 zip 文件是否有效
            if not zipfile.is_zipfile(zip_path):
                error_msg = f"Invalid ZIP file: {zip_path}"
                self._log(error_msg)
                return None
            
            self._log(f"Extracting SDE to: {extract_dir}")
            os.makedirs(extract_dir, exist_ok=True)
            
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                file_list = zip_ref.namelist()
                total_files = len(file_list)
                self._log(f"Found {total_files} files in ZIP archive")
                
                for i, member in enumerate(file_list):
                    try:
                        zip_ref.extract(member, extract_dir)
                        if progress_callback and total_files > 0:
                            progress = int((i + 1) / total_files * 100)
                            progress_callback(progress)
                    except Exception as e:
                        self._log(f"Warning: Failed to extract {member}: {str(e)}")
                        continue
            
            # 验证解压结果
            if os.path.exists(extract_dir) and os.listdir(extract_dir):
                self._log(f"SDE extracted successfully to: {extract_dir}")
                return extract_dir
            else:
                error_msg = f"Extraction completed but directory is empty: {extract_dir}"
                self._log(error_msg)
                return None
        except zipfile.BadZipFile:
            error_msg = f"Corrupted ZIP file: {zip_path}"
            self._log(error_msg)
            return None
        except zipfile.LargeZipFile:
            error_msg = f"ZIP file too large (requires ZIP64): {zip_path}"
            self._log(error_msg)
            return None
        except IOError as e:
            error_msg = f"IO error while extracting SDE: {str(e)}\nPath: {extract_dir}"
            self._log(error_msg)
            return None
        except Exception as e:
            import traceback
            error_msg = f"Unexpected error extracting SDE: {str(e)}\n{traceback.format_exc()}"
            self._log(error_msg)
            return None

    def ensure_sde_available(self, progress_callback=None):
        """确保 SDE 数据可用，如果不存在则下载并解压"""
        build_number = self.get_latest_build()
        if not build_number:
            self._log("Cannot proceed without build number")
            return None
        
        variant = "jsonl"
        
        # 检查是否已下载
        if self.is_downloaded(build_number, variant):
            self._log(f"SDE build {build_number} already available in cache")
            extract_dir = os.path.join(self.cache_dir, f"eve-online-static-data-{build_number}-{variant}")
            return extract_dir
        
        # 下载
        self._log(f"Starting download of SDE build {build_number}")
        zip_path = self.download_sde(build_number, variant, progress_callback)
        if not zip_path:
            self._log(f"Download failed for SDE build {build_number}")
            return None
        
        # 解压
        self._log(f"Starting extraction of SDE build {build_number}")
        extract_dir = self.extract_sde(zip_path, progress_callback)
        if not extract_dir:
            self._log(f"Extraction failed for SDE build {build_number}")
            return None
        
        self._log(f"SDE build {build_number} is now available at: {extract_dir}")
        return extract_dir


class SDEDownloadThread(QThread):
    """后台线程用于下载和解压 SDE 数据"""
    progress_updated = pyqtSignal(int, str)  # 进度百分比, 状态消息
    finished = pyqtSignal(str)  # 成功时返回路径
    error = pyqtSignal(str)  # 失败时返回错误消息
    
    def __init__(self, sde_downloader, parent=None):
        super().__init__(parent)
        self.sde_downloader = sde_downloader
        
    def run(self):
        """在后台线程中执行下载"""
        try:
            def progress_callback(progress):
                # 通过信号发送进度更新
                self.progress_updated.emit(progress, f"Downloading/Extracting SDE: {progress}%")
            
            self.progress_updated.emit(0, "Fetching latest SDE build number...")
            build_number = self.sde_downloader.get_latest_build()
            if not build_number:
                self.error.emit("Failed to get latest SDE build number")
                return
            
            variant = "jsonl"
            
            # 检查是否已下载
            if self.sde_downloader.is_downloaded(build_number, variant):
                self.progress_updated.emit(100, "SDE already available in cache")
                extract_dir = os.path.join(
                    self.sde_downloader.cache_dir, 
                    f"eve-online-static-data-{build_number}-{variant}"
                )
                self.finished.emit(extract_dir)
                return
            
            # 下载
            self.progress_updated.emit(10, "Starting download...")
            zip_path = self.sde_downloader.download_sde(build_number, variant, progress_callback)
            if not zip_path:
                self.error.emit("Failed to download SDE data")
                return
            
            # 解压
            self.progress_updated.emit(60, "Starting extraction...")
            extract_dir = self.sde_downloader.extract_sde(zip_path, progress_callback)
            if not extract_dir:
                self.error.emit("Failed to extract SDE data")
                return
            
            self.progress_updated.emit(100, "SDE data ready")
            self.finished.emit(extract_dir)
        except Exception as e:
            import traceback
            error_msg = f"Unexpected error: {str(e)}\n{traceback.format_exc()}"
            self.error.emit(error_msg)


class ShipTree(QTreeWidget):
    def __init__(self, parent=None, event_logger=None):
        super().__init__(parent)
        
        self.setHeaderLabel("Ships: ► ")
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self.show_context_menu)
        self.itemSelectionChanged.connect(self._show_selected_item)
        self.setHeaderLabels(["", "Size"])
        
        self.setColumnWidth(0, 775)
        self.setColumnWidth(1, 50)
        self.header().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self.setIndentation(15)  # 设置缩进为15像素，与 Local 标签页保持一致
        
        self.event_logger = event_logger
        self.sde_downloader = SDEDownloader(event_logger=event_logger)
        self.are_ships_loaded = False
        self.sde_path = None
        self.download_thread = None
        self.loading_label = None
        self.res_tree = None  # 保存 ResTree 引用，用于查找模型文件
        self.resfile_dependencies = {}  # 存储 resfiledependencies.yaml 的内容
        
        self.icon_atlas = QPixmap("./icons/icons.png")
        
        try:
            self.config = json.loads(open(CONFIG_FILE, "r", encoding="utf-8").read())
        except:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                f.write(json.dumps({"SharedCacheLocation": ""}, indent=4))
            self.config = json.loads(open(CONFIG_FILE, "r", encoding="utf-8").read())
        
        self.show()

    def _show_selected_item(self):
        try:
            item = self.selectedItems()[0]
            if hasattr(item, 'respath'):
                self.setHeaderLabel(
                    "Ships: ► " + item.respath.replace("/", " ► ")
                )
            
            # 如果是飞船目录或文件，输出详细信息
            # 飞船现在是目录节点，资源文件是文件节点
            if (isinstance(item, EVEDirectory) or isinstance(item, EVEFile)) and hasattr(item, 'type_id'):
                type_name = getattr(item, 'type_name', item.text(0))
                type_id = getattr(item, 'type_id', 'N/A')
                graphic_id = getattr(item, 'graphic_id', None)
                graphics_info = getattr(item, 'graphics_info', {})
                
                print(f"物品名称: {type_name}")
                print(f"TypeID: {type_id}")
                print(f"GraphicID: {graphic_id if graphic_id else 'N/A'}")
                print(f"iconFolder: {graphics_info.get('iconFolder', 'N/A')}")
                print(f"sofFactionName: {graphics_info.get('sofFactionName', 'N/A')}")
                print(f"sofHullName: {graphics_info.get('sofHullName', 'N/A')}")
                print(f"sofRaceName: {graphics_info.get('sofRaceName', 'N/A')}")
                
                # 使用新的依赖查找逻辑
                sof_hull_name = graphics_info.get('sofHullName', '')
                if sof_hull_name:
                    # 从依赖中提取所有 .gr2 模型文件
                    # 优先使用已保存的 model_files 属性（如果存在），否则重新查找
                    model_files = getattr(item, 'model_files', None)
                    if model_files is None:
                        model_files = self._get_model_files_from_dependencies(sof_hull_name)
                    
                    if model_files:
                        # 对模型文件进行排序
                        model_files_sorted = sorted(model_files, key=str.lower)
                        print(f"\n模型文件 (共 {len(model_files_sorted)} 个):")
                        for model_file in model_files_sorted:
                            hash_path = self._get_file_hash_path(model_file)
                            if hash_path:
                                print(f"  - {model_file} -> {hash_path}")
                            else:
                                print(f"  - {model_file} -> (未找到哈希)")
                    else:
                        print(f"\n模型文件: 未找到（依赖中无 .gr2 文件）")
                    
                    # 从依赖中提取所有贴图文件（.dds, .png, .jpg），应用过滤策略
                    icon_folder = graphics_info.get('iconFolder', '')
                    sof_race_name = graphics_info.get('sofRaceName', '')
                    texture_files = self._get_texture_files_from_dependencies(sof_hull_name, icon_folder, sof_race_name)
                    if texture_files:
                        # 对贴图文件进行排序
                        texture_files_sorted = sorted(texture_files, key=str.lower)
                        print(f"\n贴图文件 (共 {len(texture_files_sorted)} 个):")
                        for texture_file in texture_files_sorted:
                            hash_path = self._get_file_hash_path(texture_file)
                            if hash_path:
                                print(f"  - {texture_file} -> {hash_path}")
                            else:
                                print(f"  - {texture_file} -> (未找到哈希)")
                    else:
                        print(f"\n贴图文件: 未找到（依赖中无贴图文件或全部被过滤）")
                    
                    # 打印所有未过滤的依赖
                    all_dependencies = self._get_ship_dependencies(sof_hull_name)
                    if all_dependencies:
                        all_dependencies_sorted = sorted(all_dependencies, key=str.lower)
                        print(f"\n全部依赖文件 (共 {len(all_dependencies_sorted)} 个，未过滤):")
                        for dep_file in all_dependencies_sorted:
                            hash_path = self._get_file_hash_path(dep_file)
                            if hash_path:
                                print(f"  - {dep_file} -> {hash_path}")
                            else:
                                print(f"  - {dep_file} -> (未找到哈希)")
                    else:
                        print(f"\n全部依赖文件: 未找到")
                else:
                    print(f"\n模型文件: sofHullName 为空，无法查找模型")
                    print(f"贴图文件: sofHullName 为空，无法查找贴图")
                    print(f"全部依赖文件: sofHullName 为空，无法查找依赖")
                
                print("-" * 50)
        except:
            pass

    def _find_ship_root_directory(self, icon_folder, sof_hull_name):
        """
        根据 iconFolder 和 sofHullName 查找飞船的根目录
        
        方案A：
        1. 从 iconFolder 获取上级目录（去掉 /icons）
        2. 在该目录下查找名称包含 sofHullName 的 .gr2 文件
        3. 如果找到，返回根目录路径；否则返回 None
        
        例如：
        iconFolder: res:/dx9/model/ship/minmatar/frigate/mf1/icons
        上级目录: res:/dx9/model/ship/minmatar/frigate/mf1/
        sofHullName: mf1_t1
        查找: mf1_t1.gr2
        如果找到，返回: res:/dx9/model/ship/minmatar/frigate/mf1/
        """
        if not icon_folder or not sof_hull_name:
            return None
        
        if not self.res_tree or not self.res_tree.resfiles_list:
            return None
        
        # 从 iconFolder 提取上级目录
        # res:/dx9/model/ship/minmatar/frigate/mf1/icons -> dx9/model/ship/minmatar/frigate/mf1/
        if icon_folder.startswith("res:/"):
            base_path = icon_folder[5:]  # 去掉 "res:/"
        else:
            base_path = icon_folder
        
        # 去掉末尾的 /icons 或 icons
        if base_path.endswith("/icons"):
            base_path = base_path[:-6]  # 去掉 "/icons"
        else:
            return None
        # 确保路径以 / 结尾，并转换为小写用于匹配
        if not base_path.endswith("/"):
            base_path += "/"
        base_path_lower = base_path.lower()
        
        # 构建期望的文件名（完全匹配）
        expected_file_name = f"{sof_hull_name.lower()}.gr2"
        expected_full_path = f"{base_path_lower}{expected_file_name}"
        
        # 在资源文件列表中查找完全匹配的文件（只在根目录，不递归子目录）
        for resfile in self.res_tree.resfiles_list:
            res_path = resfile.get("res_path", "").lower()
            
            # 检查路径是否完全匹配（确保只在根目录下，且文件名完全匹配）
            if res_path == expected_full_path:
                # 找到模型文件，返回根目录路径（带 res:/ 前缀）
                return f"res:/{base_path}"
        
        return None
    
    def _find_model_file(self, ship_root_directory, sof_hull_name):
        """
        根据飞船目录和 sofHullName 查找模型文件
        
        参数：
        - ship_root_directory: 飞船目录，如 res:/dx9/model/ship/minmatar/frigate/mf1/
        - sof_hull_name: 船体名称，如 mf1_t1
        
        返回：模型文件信息（包含 res_path, size），如果未找到返回 None
        
        注意：
        - 只在根目录下查找，不递归子目录
        - 文件名必须与 sofHullName 完全匹配（不区分大小写）
        """
        if not ship_root_directory or not sof_hull_name:
            return None
        
        if not self.res_tree or not self.res_tree.resfiles_list:
            return None
        
        # 从根目录提取路径（去掉 res:/ 前缀）
        if ship_root_directory.startswith("res:/"):
            base_path = ship_root_directory[5:]  # 去掉 "res:/"
        else:
            base_path = ship_root_directory
        
        # 确保路径以 / 结尾，并转换为小写用于匹配
        if not base_path.endswith("/"):
            base_path += "/"
        base_path_lower = base_path.lower()
        
        # 构建期望的文件名（完全匹配）
        expected_file_name = f"{sof_hull_name.lower()}.gr2"
        expected_full_path = f"{base_path_lower}{expected_file_name}"
        
        # 在资源文件列表中查找完全匹配的文件
        for resfile in self.res_tree.resfiles_list:
            res_path = resfile.get("res_path", "").lower()
            
            # 检查路径是否完全匹配（只在根目录，不递归子目录）
            if res_path == expected_full_path:
                return resfile
        
        return None
    
    def _load_resfile_dependencies(self, build_number):
        """
        加载并缓存 resfiledependencies.yaml 文件（在后台线程中执行）
        
        参数:
        - build_number: 构建号
        
        返回:
        - bool: 是否成功加载（如果缓存存在则立即返回，否则启动后台下载）
        """
        # 使用与 SDE 相同的缓存目录，确保文件与 SDE 数据一致
        cache_dir = self.sde_downloader.cache_dir  # 使用 SDEDownloader 的缓存目录
        os.makedirs(cache_dir, exist_ok=True)
        # 文件名格式：resfiledependencies_{build_number}.yaml，与 SDE 数据保持一致
        cache_file = os.path.join(cache_dir, f"resfiledependencies_{build_number}.yaml")
        
        # 检查缓存是否存在
        if os.path.exists(cache_file):
            try:
                self.event_logger.add(f"Loading cached resfiledependencies.yaml from {cache_file}")
                with open(cache_file, 'r', encoding='utf-8') as f:
                    self.resfile_dependencies = yaml.safe_load(f) or {}
                self.event_logger.add(f"Loaded {len(self.resfile_dependencies)} dependencies from cache")
                return True
            except Exception as e:
                self.event_logger.add(f"Error loading cached file: {str(e)}")
        
        # 缓存不存在，在后台线程中下载
        self.event_logger.add("Starting background download of resfiledependencies.yaml...")
        download_thread = ResFileDependenciesDownloadThread(build_number, cache_file, self.event_logger)
        download_thread.download_complete.connect(self._on_resfile_dependencies_downloaded)
        download_thread.start()
        return False  # 返回 False 表示正在后台下载
    
    def _on_resfile_dependencies_downloaded(self, success, build_number, cache_file, dependencies):
        """处理 resfiledependencies.yaml 下载完成的回调"""
        if success:
            self.resfile_dependencies = dependencies
            self.event_logger.add(f"Background download completed: Loaded {len(self.resfile_dependencies)} dependencies")
        else:
            self.event_logger.add("Background download of resfiledependencies.yaml failed")
    
    def _get_ship_dependencies(self, sof_hull_name):
        """
        根据 sofHullName 获取飞船的所有依赖文件
        
        参数:
        - sof_hull_name: 船体名称，如 mf1_t1
        
        返回:
        - list: 依赖文件路径列表，如果未找到返回空列表
        """
        if not sof_hull_name or not self.resfile_dependencies:
            return []
        
        # 构建 key: res:/dx9/model/spaceobjectfactory/hulls/<sofHullName>.red
        key = f"res:/dx9/model/spaceobjectfactory/hulls/{sof_hull_name}.red"
        
        # 完全匹配查找
        dependencies = self.resfile_dependencies.get(key, [])
        return dependencies if isinstance(dependencies, list) else []
    
    def _get_model_files_from_dependencies(self, sof_hull_name):
        """
        从依赖列表中提取所有 .gr2 模型文件
        
        参数:
        - sof_hull_name: 船体名称，如 mf1_t1
        
        返回:
        - list: .gr2 文件路径列表，如果未找到返回空列表
        """
        dependencies = self._get_ship_dependencies(sof_hull_name)
        if not dependencies:
            return []
        
        # 使用统一的过滤函数，所有 .gr2 文件都会被过滤（返回 True）
        model_files = [dep for dep in dependencies if dep.lower().endswith('.gr2')]
        return model_files
    
    def _get_file_hash_path(self, respath):
        """
        从 resfiles_list 中查找文件的哈希路径
        
        参数:
        - respath: 资源路径，如 res:/dx9/model/ship/amarr/frigate/af1/af1_t1.gr2
        
        返回:
        - str: 哈希路径，如 cd/cdeabdd06e0dedaa_6fa910a9ccb6af0d5de4b5a95119eac7，如果未找到返回 None
        """
        if not self.res_tree or not self.res_tree.resfiles_list:
            return None
        
        # 标准化路径
        respath_normalized = respath.lower()
        if respath_normalized.startswith("res:/"):
            respath_normalized = respath_normalized[5:]  # 去掉 "res:/"
        
        # 在 resfiles_list 中查找
        for resfile in self.res_tree.resfiles_list:
            res_path = resfile.get("res_path", "").lower()
            if res_path == respath_normalized:
                resfile_hash = resfile.get("resfile_hash", "")
                if resfile_hash:
                    # resfile_hash 已经是 folder/hash 格式，直接返回
                    return resfile_hash
                break
        
        return None
    
    def _get_texture_files_from_dependencies(self, sof_hull_name, icon_folder=None, sof_race_name=None):
        """
        从依赖列表中提取所有贴图文件（.dds, .png, .jpg），并应用过滤策略
        
        参数:
        - sof_hull_name: 船体名称，如 mf1_t1
        - icon_folder: 图标文件夹路径，如 res:/dx9/model/ship/amarr/titan/at1/icons
        - sof_race_name: 种族名称，如 amarr
        
        返回:
        - list: 贴图文件路径列表，如果未找到返回空列表
        """
        dependencies = self._get_ship_dependencies(sof_hull_name)
        if not dependencies:
            return []
        
        # 使用统一的过滤函数来过滤贴图文件
        texture_files = []
        for dep in dependencies:
            dep_lower = dep.lower()
            # 只处理贴图文件（.dds, .png, .jpg）
            if dep_lower.endswith('.dds') or dep_lower.endswith('.png') or dep_lower.endswith('.jpg'):
                # 确保路径是完整的 res:/ 格式
                full_dep_path = dep if dep.startswith("res:/") else f"res:/{dep}"
                # 使用统一的过滤函数
                if self._is_filtered_file(full_dep_path, icon_folder, sof_race_name):
                    texture_files.append(dep)

        return texture_files
    
    def _find_faction_directory_files(self, ship_root_directory, sof_faction_name, sof_hull_name=None, icon_folder=None, sof_race_name=None):
        """
        在飞船根目录下查找与 sofFactionName 同名的目录，并返回该目录下的所有文件（应用过滤）
        
        参数:
        - ship_root_directory: 飞船根目录，如 res:/dx9/model/ship/amarr/frigate/af1/
        - sof_faction_name: 派系名称，如 amarr
        - sof_hull_name: 船体名称，如 af1_t1，用于按文件名过滤
        - icon_folder: 图标文件夹路径，用于过滤判断
        - sof_race_name: 种族名称，用于过滤判断
        
        返回:
        - list: 文件路径列表（已过滤），如果未找到目录或文件则返回空列表
        """
        if not ship_root_directory or not sof_faction_name:
            return []
        
        if not self.res_tree or not self.res_tree.resfiles_list:
            return []
        
        # 从根目录提取路径（去掉 res:/ 前缀）
        if ship_root_directory.startswith("res:/"):
            base_path = ship_root_directory[5:]  # 去掉 "res:/"
        else:
            base_path = ship_root_directory
        
        # 确保路径以 / 结尾
        if not base_path.endswith("/"):
            base_path += "/"
        
        # 构建 faction 目录路径
        faction_dir_path = f"{base_path}{sof_faction_name.lower()}/"
        faction_dir_path_lower = faction_dir_path.lower()
        
        # 准备 sofHullName 过滤（如果提供）
        sof_hull_name_lower = sof_hull_name.lower() if sof_hull_name else None
        
        # 在资源文件列表中查找该目录下的所有文件，并应用过滤
        faction_files = []
        for resfile in self.res_tree.resfiles_list:
            res_path = resfile.get("res_path", "").lower()
            
            # 检查路径是否在 faction 目录下
            if res_path.startswith(faction_dir_path_lower):
                # 构建完整的 res:/ 路径
                full_path = f"res:/{resfile.get('res_path', '')}"
                
                # 首先按 sofHullName 过滤（如果提供）
                if sof_hull_name_lower:
                    file_name = os.path.basename(full_path).lower()
                    # 检查文件名中是否包含 sofHullName
                    if sof_hull_name_lower not in file_name:
                        continue  # 跳过不包含 sofHullName 的文件
                
                # 应用过滤逻辑（与贴图文件使用相同的过滤规则）
                if self._is_filtered_file(full_path, icon_folder, sof_race_name):
                    faction_files.append(full_path)
        
        return faction_files
    
    def set_icon_from_extension(self, ext):
        """根据文件扩展名设置图标"""
        if ext == ".png":
            return QIcon(self.icon_atlas.copy(97, 0, 15, 16))
        elif ext == ".dds":
            return QIcon(self.icon_atlas.copy(33, 0, 15, 16))
        elif ext == ".jpg":
            return QIcon(self.icon_atlas.copy(81, 0, 15, 16))
        elif ext == ".gr2":
            return QIcon(self.icon_atlas.copy(177, 0, 15, 16))
        elif ext in (".txt", ".yaml", ".xml", ".json"):
            return QIcon(self.icon_atlas.copy(130, 0, 15, 16))
        elif ext == ".webm":
            return QIcon(self.icon_atlas.copy(65, 0, 15, 16))
        else:
            return QIcon(self.icon_atlas.copy(161, 0, 15, 16))
    
    def _add_faction_directory_to_ship(self, ship_dir, ship_root_directory, sof_faction_name, sof_hull_name=None, icon_folder=None, sof_race_name=None):
        """
        为飞船目录添加 faction 目录及其文件（应用过滤）
        
        参数:
        - ship_dir: 飞船目录节点
        - ship_root_directory: 飞船根目录，如 res:/dx9/model/ship/amarr/frigate/af1/
        - sof_faction_name: 派系名称，如 amarr
        - sof_hull_name: 船体名称，如 af1_t1，用于按文件名过滤
        - icon_folder: 图标文件夹路径，用于过滤判断
        - sof_race_name: 种族名称，用于过滤判断
        """
        if not ship_root_directory or not sof_faction_name:
            return
        
        # 查找 faction 目录下的文件（应用过滤，包括按 sofHullName 过滤）
        faction_files = self._find_faction_directory_files(ship_root_directory, sof_faction_name, sof_hull_name, icon_folder, sof_race_name)
        
        if not faction_files:
            return
        
        # 创建 faction 目录节点
        faction_dir = EVEDirectory(
            ship_dir,
            text=sof_faction_name,
            icon=QIcon(self.icon_atlas.copy(16, 0, 15, 16))
        )
        ship_dir.add(faction_dir)
        
        # 添加 faction 目录下的所有文件
        for faction_file in sorted(faction_files, key=str.lower):
            file_ext = os.path.splitext(faction_file)[1]
            faction_item = EVEFile(
                faction_dir,
                text=os.path.basename(faction_file),
                filename=os.path.basename(faction_file),
                respath=faction_file,
                resfile_hash="",
                size=0,
                icon=self.set_icon_from_extension(file_ext),
            )
            faction_dir.add(faction_item)
    
    def _format_filesize(self, size):
        """格式化文件大小"""
        size = float(size)
        for unit in ["KB", "MB", "GB"]:
            size /= 1024
            if size <= 1024:
                return f"{size:.2f} {unit}"
        return f"{size:.2f} GB"
    
    def _start_sde_download(self):
        """启动 SDE 下载（内部方法，可被外部调用）"""
        # 如果已经在下载中，不重复启动
        if self.download_thread and self.download_thread.isRunning():
            return
        
        self.clear()
        root = EVEDirectory(self, "Ships:", QIcon("./icons/res.png"))
        root.setExpanded(True)
        
        # 显示加载进度标签
        if self.loading_label is None:
            self.loading_label = QLabel("Fetching latest SDE data...", self)
            self.loading_label.setGeometry(25, 25, 500, 50)
            self.loading_label.setStyleSheet("color: white; font-weight: bold; background-color: rgba(0,0,0,128); padding: 5px;")
        self.loading_label.setText("Fetching latest SDE data...")
        self.loading_label.show()
        
        # 创建并启动下载线程
        self.download_thread = SDEDownloadThread(self.sde_downloader, self)
        self.download_thread.progress_updated.connect(self._on_download_progress)
        self.download_thread.finished.connect(lambda path: self._on_download_finished(path, root))
        self.download_thread.error.connect(self._on_download_error)
        self.download_thread.start()
    
    def load_ships(self):
        """加载飞船资源文件"""
        if self.are_ships_loaded:
            return
        
        # 启动 SDE 下载
        self._start_sde_download()
    
    def _on_download_progress(self, progress, message):
        """更新下载进度"""
        if self.loading_label:
            self.loading_label.setText(message)
        QApplication.processEvents()
    
    def _on_download_finished(self, sde_path, root):
        """下载完成后的处理"""
        if self.loading_label:
            self.loading_label.hide()
        
        self.sde_path = sde_path
        self.event_logger.add(f"SDE data loaded successfully from: {sde_path}")
        
        # 从路径中提取 build_number
        # 路径格式: ./sde_cache/eve-online-static-data-{build_number}-jsonl
        build_number = None
        try:
            match = re.search(r'eve-online-static-data-(\d+)-jsonl', sde_path)
            if match:
                build_number = match.group(1)
                self.event_logger.add(f"Extracted build number: {build_number}")
            else:
                # 如果无法从路径提取，尝试获取最新的 build number
                build_number = self.sde_downloader.get_latest_build()
                if build_number:
                    self.event_logger.add(f"Using latest build number: {build_number}")
        except Exception as e:
            self.event_logger.add(f"Warning: Could not extract build number: {str(e)}")
        
        # 加载 resfiledependencies.yaml（如果成功获取 build_number）
        if build_number:
            self._load_resfile_dependencies(build_number)
        else:
            self.event_logger.add("Warning: Cannot load resfiledependencies.yaml without build number")
        
        # 加载飞船数据
        try:
            self._load_ship_files(root)
            self.are_ships_loaded = True
        except Exception as e:
            import traceback
            error_msg = f"Error loading ship files: {str(e)}\n{traceback.format_exc()}"
            self.event_logger.add(error_msg)
            error_label = QLabel("Error loading ship files. Check Logs for details.", self)
            error_label.setGeometry(25, 25, 500, 50)
            error_label.setStyleSheet("color: red; font-weight: bold; background-color: rgba(0,0,0,128); padding: 5px;")
            error_label.show()
    
    def _on_download_error(self, error_msg):
        """下载失败后的处理"""
        if self.loading_label:
            self.loading_label.hide()
        
        self.event_logger.add(f"SDE download failed: {error_msg}")
        error_label = QLabel("Failed to load SDE data. Check Logs for details.", self)
        error_label.setGeometry(25, 25, 500, 50)
        error_label.setStyleSheet("color: red; font-weight: bold; background-color: rgba(0,0,0,128); padding: 5px;")
        error_label.show()

    def _load_ship_files(self, root):
        """从 SDE 数据中加载飞船信息"""
        try:
            # 查找 groups.jsonl、types.jsonl 和 graphics.jsonl 文件
            groups_path = None
            types_path = None
            graphics_path = None
            
            for root_dir, dirs, files in os.walk(self.sde_path):
                for file in files:
                    file_lower = file.lower()
                    if file_lower == 'groups.jsonl':
                        groups_path = os.path.join(root_dir, file)
                    elif file_lower == 'types.jsonl':
                        types_path = os.path.join(root_dir, file)
                    elif file_lower == 'graphics.jsonl':
                        graphics_path = os.path.join(root_dir, file)
                if groups_path and types_path and graphics_path:
                    break
            
            if not groups_path:
                self.event_logger.add("groups.jsonl file not found in SDE data")
                return
            
            if not types_path:
                self.event_logger.add("types.jsonl file not found in SDE data")
                return
            
            if not graphics_path:
                self.event_logger.add("graphics.jsonl file not found in SDE data")
                return
            
            # 读取飞船组信息（categoryID = 6）
            ship_groups = {}  # {group_id: group_name}
            self.event_logger.add("Reading groups.jsonl...")
            
            try:
                with open(groups_path, 'r', encoding='utf-8') as f:
                    for line_num, line in enumerate(f, 1):
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                            category_id = data.get('categoryID', 0)
                            if category_id == 6:
                                group_id = data.get('_key')
                                name_obj = data.get('name', {})
                                group_name = name_obj.get('en', f"Group {group_id}") if isinstance(name_obj, dict) else str(name_obj)
                                if group_id is not None:
                                    ship_groups[int(group_id)] = group_name
                        except json.JSONDecodeError as e:
                            self.event_logger.add(f"Warning: Failed to parse line {line_num} in groups.jsonl: {str(e)}")
                            continue
            except Exception as e:
                self.event_logger.add(f"Error reading groups.jsonl: {str(e)}")
                return
            
            if not ship_groups:
                self.event_logger.add("No ship groups found (categoryID = 6)")
                return
            
            self.event_logger.add(f"Found {len(ship_groups)} ship groups")
            
            # 读取 graphics.jsonl 建立 graphicID 到素材信息的映射
            graphics_info = {}  # {graphic_id: {iconFolder, sofFactionName, sofHullName, sofRaceName}}
            self.event_logger.add("Reading graphics.jsonl...")
            
            try:
                with open(graphics_path, 'r', encoding='utf-8') as f:
                    for line_num, line in enumerate(f, 1):
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                            graphic_id = data.get('_key')
                            if graphic_id is not None:
                                graphics_info[int(graphic_id)] = {
                                    'iconFolder': data.get('iconFolder', ''),
                                    'sofFactionName': data.get('sofFactionName', ''),
                                    'sofHullName': data.get('sofHullName', ''),
                                    'sofRaceName': data.get('sofRaceName', '')
                                }
                        except json.JSONDecodeError as e:
                            self.event_logger.add(f"Warning: Failed to parse line {line_num} in graphics.jsonl: {str(e)}")
                            continue
            except Exception as e:
                self.event_logger.add(f"Error reading graphics.jsonl: {str(e)}")
                return
            
            self.event_logger.add(f"Loaded {len(graphics_info)} graphics entries")
            
            # 读取飞船类型信息
            ships_by_group = {}  # {group_id: [(type_id, type_name, published, graphic_id), ...]}
            self.event_logger.add("Reading types.jsonl...")
            
            ship_group_ids = set(ship_groups.keys())
            ship_count = 0
            
            try:
                with open(types_path, 'r', encoding='utf-8') as f:
                    for line_num, line in enumerate(f, 1):
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                            group_id = data.get('groupID', 0)
                            if int(group_id) in ship_group_ids:
                                type_id = data.get('_key')
                                name_obj = data.get('name', {})
                                type_name = name_obj.get('en', f"Type {type_id}") if isinstance(name_obj, dict) else str(name_obj)
                                published = data.get('published', False)
                                graphic_id = data.get('graphicID')
                                if type_id is not None:
                                    if group_id not in ships_by_group:
                                        ships_by_group[group_id] = []
                                    ships_by_group[group_id].append((int(type_id), type_name, published, graphic_id))
                                    ship_count += 1
                        except json.JSONDecodeError as e:
                            self.event_logger.add(f"Warning: Failed to parse line {line_num} in types.jsonl: {str(e)}")
                            continue
            except Exception as e:
                self.event_logger.add(f"Error reading types.jsonl: {str(e)}")
                return
            
            self.event_logger.add(f"Found {ship_count} ships in {len(ships_by_group)} groups")
            
            # 分离已发布和未发布的飞船
            published_ships_by_group = {}  # {group_id: [(type_id, type_name, published, graphic_id), ...]}
            unpublished_ships = []  # [(type_id, type_name, group_id, group_name, graphic_id), ...]
            
            for group_id, ships in ships_by_group.items():
                published_ships = []
                for type_id, type_name, published, graphic_id in ships:
                    if published:
                        published_ships.append((type_id, type_name, published, graphic_id))
                    else:
                        group_name = ship_groups.get(group_id, f"Group {group_id}")
                        unpublished_ships.append((type_id, type_name, group_id, group_name, graphic_id))
                
                if published_ships:
                    published_ships_by_group[group_id] = published_ships
            
            # 只显示有已发布飞船的组，按组名称字母顺序排序，构建树状结构
            # 创建 (group_id, group_name) 元组列表，按名称排序
            groups_to_display = [(gid, ship_groups[gid]) for gid in ship_groups.keys() if gid in published_ships_by_group]
            sorted_groups = sorted(groups_to_display, key=lambda x: x[1].lower())  # 不区分大小写的字母顺序排序
            
            # 收集没有找到模型的飞船
            ships_without_model = []  # [(type_id, type_name, group_id, group_name, graphic_id, graphics_info_item), ...]
            # 收集多模型飞船（.gr2 文件数量超过 2 个）
            multi_model_ships = []  # [(type_id, type_name, group_id, group_name, graphic_id, graphics_info_item, model_files), ...]
            
            # 添加已发布的组
            for group_id, group_name in sorted_groups:
                
                # 创建组目录
                group_item = EVEDirectory(
                    root, 
                    text=group_name, 
                    icon=QIcon(self.icon_atlas.copy(16, 0, 15, 16))
                )
                group_item.group_id = group_id
                root.add(group_item)
                
                # 添加该组下的已发布飞船
                # 按飞船名称字母顺序排序
                ships = sorted(published_ships_by_group[group_id], key=lambda x: x[1].lower())  # x[1] 是 type_name
                
                for type_id, type_name, published, graphic_id in ships:
                    # 获取图形信息
                    graphics_info_item = graphics_info.get(graphic_id, {}) if graphic_id else {}
                    sof_hull_name = graphics_info_item.get('sofHullName', '')
                    
                    # 从依赖中查找模型文件（.gr2 文件）
                    model_files = []
                    if sof_hull_name:
                        model_files = self._get_model_files_from_dependencies(sof_hull_name)
                    
                    # 获取贴图文件
                    icon_folder = graphics_info_item.get('iconFolder', '')
                    sof_race_name = graphics_info_item.get('sofRaceName', '')
                    sof_faction_name = graphics_info_item.get('sofFactionName', '')
                    texture_files = []
                    if sof_hull_name:
                        texture_files = self._get_texture_files_from_dependencies(sof_hull_name, icon_folder, sof_race_name)
                    
                    # 获取飞船根目录
                    ship_root_directory = None
                    if icon_folder and sof_hull_name:
                        ship_root_directory = self._find_ship_root_directory(icon_folder, sof_hull_name)
                    
                    # 如果没有找到模型，收集到列表中
                    if not model_files:
                        ships_without_model.append((type_id, type_name, group_id, group_name, graphic_id, graphics_info_item))
                    elif len(model_files) > 2:
                        # 如果模型文件数量超过 2 个，收集到多模型列表中
                        multi_model_ships.append((type_id, type_name, group_id, group_name, graphic_id, graphics_info_item, model_files))
                    else:
                        # 找到模型的飞船创建为目录节点（模型数量 <= 2）
                        ship_dir = EVEDirectory(
                            group_item,
                            text=type_name,
                            icon=QIcon(self.icon_atlas.copy(16, 0, 15, 16))
                        )
                        ship_dir.type_id = type_id
                        ship_dir.type_name = type_name
                        ship_dir.group_id = group_id
                        ship_dir.published = published
                        ship_dir.graphic_id = graphic_id
                        ship_dir.graphics_info = graphics_info_item
                        ship_dir.model_files = model_files  # 保存模型文件列表
                        group_item.add(ship_dir)
                        
                        # 添加模型文件
                        for model_file in sorted(model_files, key=str.lower):
                            model_item = EVEFile(
                                ship_dir,
                                text=os.path.basename(model_file),
                                filename=os.path.basename(model_file),
                                respath=model_file,
                                resfile_hash="",
                                size=0,
                                icon=QIcon(self.icon_atlas.copy(177, 0, 15, 16)),
                            )
                            ship_dir.add(model_item)
                        
                        # 添加贴图文件
                        for texture_file in sorted(texture_files, key=str.lower):
                            texture_item = EVEFile(
                                ship_dir,
                                text=os.path.basename(texture_file),
                                filename=os.path.basename(texture_file),
                                respath=texture_file,
                                resfile_hash="",
                                size=0,
                                icon=self.set_icon_from_extension(os.path.splitext(texture_file)[1]),
                            )
                            ship_dir.add(texture_item)
                        
                        # 添加 faction 目录（如果存在）
                        if ship_root_directory and sof_faction_name:
                            self._add_faction_directory_to_ship(ship_dir, ship_root_directory, sof_faction_name, sof_hull_name, icon_folder, sof_race_name)
            
            # 如果有未发布的飞船，创建专门的组放在最后
            if unpublished_ships:
                unpublished_group_item = EVEDirectory(
                    root,
                    text="Unpublished",
                    icon=QIcon(self.icon_atlas.copy(16, 0, 15, 16))
                )
                unpublished_group_item.group_id = -1  # 使用特殊ID标识未发布组
                root.add(unpublished_group_item)
                
                # 按飞船名称字母顺序排序未发布的飞船
                unpublished_ships.sort(key=lambda x: x[1].lower())  # x[1] 是 type_name
                
                for type_id, type_name, group_id, group_name, graphic_id in unpublished_ships:
                    # 获取图形信息
                    graphics_info_item = graphics_info.get(graphic_id, {}) if graphic_id else {}
                    sof_hull_name = graphics_info_item.get('sofHullName', '')
                    
                    # 从依赖中查找模型文件（.gr2 文件）
                    model_files = []
                    if sof_hull_name:
                        model_files = self._get_model_files_from_dependencies(sof_hull_name)
                    
                    # 获取贴图文件
                    icon_folder = graphics_info_item.get('iconFolder', '')
                    sof_race_name = graphics_info_item.get('sofRaceName', '')
                    sof_faction_name = graphics_info_item.get('sofFactionName', '')
                    texture_files = []
                    if sof_hull_name:
                        texture_files = self._get_texture_files_from_dependencies(sof_hull_name, icon_folder, sof_race_name)
                    
                    # 获取飞船根目录
                    ship_root_directory = None
                    if icon_folder and sof_hull_name:
                        ship_root_directory = self._find_ship_root_directory(icon_folder, sof_hull_name)
                    
                    # 未发布的飞船，无论是否有模型，都添加到 Unpublished 组（作为目录）
                    ship_dir = EVEDirectory(
                        unpublished_group_item,
                        text=f"{type_name} ({group_name})",
                        icon=QIcon(self.icon_atlas.copy(16, 0, 15, 16))
                    )
                    ship_dir.type_id = type_id
                    ship_dir.type_name = type_name
                    ship_dir.group_id = group_id
                    ship_dir.published = False
                    ship_dir.graphic_id = graphic_id
                    ship_dir.graphics_info = graphics_info_item
                    ship_dir.model_files = model_files  # 保存模型文件列表
                    unpublished_group_item.add(ship_dir)
                    
                    # 添加模型文件
                    for model_file in sorted(model_files, key=str.lower):
                        model_item = EVEFile(
                            ship_dir,
                            text=os.path.basename(model_file),
                            filename=os.path.basename(model_file),
                            respath=model_file,
                            resfile_hash="",
                            size=0,
                            icon=QIcon(self.icon_atlas.copy(177, 0, 15, 16)),
                        )
                        ship_dir.add(model_item)
                    
                    # 添加贴图文件
                    for texture_file in sorted(texture_files, key=str.lower):
                        texture_item = EVEFile(
                            ship_dir,
                            text=os.path.basename(texture_file),
                            filename=os.path.basename(texture_file),
                            respath=texture_file,
                            resfile_hash="",
                            size=0,
                            icon=self.set_icon_from_extension(os.path.splitext(texture_file)[1]),
                        )
                        ship_dir.add(texture_item)
                    
                    # 添加 faction 目录（如果存在）
                    if ship_root_directory and sof_faction_name:
                        self._add_faction_directory_to_ship(ship_dir, ship_root_directory, sof_faction_name, sof_hull_name, icon_folder, sof_race_name)
                    
                    # 如果没有找到模型，同时收集到 Model Not Found 列表中
                    if not model_files:
                        ships_without_model.append((type_id, type_name, group_id, group_name, graphic_id, graphics_info_item))
                    elif len(model_files) > 2:
                        # 如果模型文件数量超过 2 个，同时收集到多模型列表中
                        multi_model_ships.append((type_id, type_name, group_id, group_name, graphic_id, graphics_info_item, model_files))
            
            # 如果有未找到模型的飞船，创建 "Model Not Found" 组放在最后
            if ships_without_model:
                model_not_found_group_item = EVEDirectory(
                    root,
                    text="Model Not Found",
                    icon=QIcon(self.icon_atlas.copy(16, 0, 15, 16))
                )
                model_not_found_group_item.group_id = -2  # 使用特殊ID标识 Model Not Found 组
                root.add(model_not_found_group_item)
                
                # 按飞船名称字母顺序排序
                ships_without_model.sort(key=lambda x: x[1].lower())  # x[1] 是 type_name
                
                for type_id, type_name, group_id, group_name, graphic_id, graphics_info_item in ships_without_model:
                    # 判断是否已发布（group_id != -1 表示不是未发布组）
                    is_published = (group_id != -1)
                    
                    # 获取贴图文件（即使没有模型，也可能有贴图）
                    sof_hull_name = graphics_info_item.get('sofHullName', '')
                    icon_folder = graphics_info_item.get('iconFolder', '')
                    sof_race_name = graphics_info_item.get('sofRaceName', '')
                    sof_faction_name = graphics_info_item.get('sofFactionName', '')
                    texture_files = []
                    if sof_hull_name:
                        texture_files = self._get_texture_files_from_dependencies(sof_hull_name, icon_folder, sof_race_name)
                    
                    # 获取飞船根目录
                    ship_root_directory = None
                    if icon_folder and sof_hull_name:
                        ship_root_directory = self._find_ship_root_directory(icon_folder, sof_hull_name)
                    
                    # Model Not Found 的飞船也创建为目录
                    ship_dir = EVEDirectory(
                        model_not_found_group_item,
                        text=f"{type_name} ({group_name})",
                        icon=QIcon(self.icon_atlas.copy(16, 0, 15, 16))
                    )
                    ship_dir.type_id = type_id
                    ship_dir.type_name = type_name
                    ship_dir.group_id = group_id
                    ship_dir.published = is_published
                    ship_dir.graphic_id = graphic_id
                    ship_dir.graphics_info = graphics_info_item
                    ship_dir.model_files = []  # 明确设置为空列表（无模型文件）
                    model_not_found_group_item.add(ship_dir)
                    
                    # 添加贴图文件（如果有）
                    for texture_file in sorted(texture_files, key=str.lower):
                        texture_item = EVEFile(
                            ship_dir,
                            text=os.path.basename(texture_file),
                            filename=os.path.basename(texture_file),
                            respath=texture_file,
                            resfile_hash="",
                            size=0,
                            icon=self.set_icon_from_extension(os.path.splitext(texture_file)[1]),
                        )
                        ship_dir.add(texture_item)
                    
                    # 添加 faction 目录（如果存在）
                    if ship_root_directory and sof_faction_name:
                        self._add_faction_directory_to_ship(ship_dir, ship_root_directory, sof_faction_name, sof_hull_name, icon_folder, sof_race_name)
            
            # 如果有多模型飞船，创建 "Multi-Model" 组放在最后
            if multi_model_ships:
                multi_model_group_item = EVEDirectory(
                    root,
                    text="Multi-Model",
                    icon=QIcon(self.icon_atlas.copy(16, 0, 15, 16))
                )
                multi_model_group_item.group_id = -3  # 使用特殊ID标识 Multi-Model 组
                root.add(multi_model_group_item)
                
                # 按飞船名称字母顺序排序
                multi_model_ships.sort(key=lambda x: x[1].lower())  # x[1] 是 type_name
                
                for type_id, type_name, group_id, group_name, graphic_id, graphics_info_item, model_files in multi_model_ships:
                    # 判断是否已发布（group_id != -1 表示不是未发布组）
                    is_published = (group_id != -1)
                    
                    # 获取贴图文件
                    sof_hull_name = graphics_info_item.get('sofHullName', '')
                    icon_folder = graphics_info_item.get('iconFolder', '')
                    sof_race_name = graphics_info_item.get('sofRaceName', '')
                    sof_faction_name = graphics_info_item.get('sofFactionName', '')
                    texture_files = []
                    if sof_hull_name:
                        texture_files = self._get_texture_files_from_dependencies(sof_hull_name, icon_folder, sof_race_name)
                    
                    # 获取飞船根目录
                    ship_root_directory = None
                    if icon_folder and sof_hull_name:
                        ship_root_directory = self._find_ship_root_directory(icon_folder, sof_hull_name)
                    
                    # Multi-Model 的飞船也创建为目录
                    ship_dir = EVEDirectory(
                        multi_model_group_item,
                        text=f"{type_name} ({group_name}) [{len(model_files)} models]",
                        icon=QIcon(self.icon_atlas.copy(16, 0, 15, 16))
                    )
                    ship_dir.type_id = type_id
                    ship_dir.type_name = type_name
                    ship_dir.group_id = group_id
                    ship_dir.published = is_published
                    ship_dir.graphic_id = graphic_id
                    ship_dir.graphics_info = graphics_info_item
                    ship_dir.model_files = model_files  # 保存模型文件列表
                    multi_model_group_item.add(ship_dir)
                    
                    # 添加模型文件
                    for model_file in sorted(model_files, key=str.lower):
                        model_item = EVEFile(
                            ship_dir,
                            text=os.path.basename(model_file),
                            filename=os.path.basename(model_file),
                            respath=model_file,
                            resfile_hash="",
                            size=0,
                            icon=QIcon(self.icon_atlas.copy(177, 0, 15, 16)),
                        )
                        ship_dir.add(model_item)
                    
                    # 添加贴图文件
                    for texture_file in sorted(texture_files, key=str.lower):
                        texture_item = EVEFile(
                            ship_dir,
                            text=os.path.basename(texture_file),
                            filename=os.path.basename(texture_file),
                            respath=texture_file,
                            resfile_hash="",
                            size=0,
                            icon=self.set_icon_from_extension(os.path.splitext(texture_file)[1]),
                        )
                        ship_dir.add(texture_item)
                    
                    # 添加 faction 目录（如果存在）
                    if ship_root_directory and sof_faction_name:
                        self._add_faction_directory_to_ship(ship_dir, ship_root_directory, sof_faction_name, sof_hull_name, icon_folder, sof_race_name)
            
            # 更新日志信息
            log_parts = []
            if unpublished_ships:
                log_parts.append(f"1 Unpublished group ({len(unpublished_ships)} ships)")
            if ships_without_model:
                log_parts.append(f"1 Model Not Found group ({len(ships_without_model)} ships)")
            if multi_model_ships:
                log_parts.append(f"1 Multi-Model group ({len(multi_model_ships)} ships)")
            
            if log_parts:
                extra_groups = " + ".join(log_parts)
                self.event_logger.add(f"Successfully loaded {ship_count} ships in {len(sorted_groups)} groups + {extra_groups}")
            else:
                if unpublished_ships:
                    self.event_logger.add(f"Successfully loaded {ship_count} ships in {len(sorted_groups)} groups + 1 Unpublished group ({len(unpublished_ships)} unpublished ships)")
                else:
                    self.event_logger.add(f"Successfully loaded {ship_count} ships in {len(sorted_groups)} groups")
        except Exception as e:
            self.event_logger.add(f"Error loading ship files: {str(e)}")
            import traceback
            self.event_logger.add(traceback.format_exc())

    def _format_filesize(self, size):
        size = float(size)
        for unit in ["KB", "MB", "GB"]:
            size /= 1024
            if size <= 1024:
                return f"{size:.2f} {unit}"

    def show_context_menu(self, point):
        item = self.itemAt(point)
        if item:
            menu = QMenu(self)
            
            if isinstance(item, EVEDirectory) and item.text(0) != "Ships:":
                save_folder_action = menu.addAction("Save folder")
                save_folder_action.triggered.connect(lambda: self._save_folder_command(item))
                menu.addSeparator()
                save_folder_and_convert_all_action = menu.addAction("Save folder | convert dds -> png, gr2 -> obj")
                save_folder_and_convert_all_action.triggered.connect(lambda: self._save_folder_command(item, convert_dds=True, convert_gr2=True))
                # 检查是否是飞船目录（有 graphics_info 属性）
                if hasattr(item, 'graphics_info') and item.graphics_info:
                    menu.addSeparator()
                    export_all_deps_action = menu.addAction("Export all dependencies (unfiltered)")
                    export_all_deps_action.triggered.connect(lambda: self._export_all_dependencies_command(item, convert_dds=False, convert_gr2=False))
                    export_all_deps_convert_action = menu.addAction("Export all dependencies | convert dds -> png, gr2 -> obj")
                    export_all_deps_convert_action.triggered.connect(lambda: self._export_all_dependencies_command(item, convert_dds=True, convert_gr2=True))
            elif isinstance(item, EVEFile):
                sub_menu = QMenu("Export...", menu)
                sub_menu.installEventFilter(ContextMenuFilter(sub_menu))
                menu.addMenu(sub_menu)
                save_file_action = sub_menu.addAction("Save file")
                save_file_action.triggered.connect(lambda: self._save_file_command(item))
                sub_menu.addSeparator()
                
                if item.text(0).endswith(".gr2"):
                    export_obj_action = sub_menu.addAction("Save as .obj")
                    export_obj_action.triggered.connect(lambda: self._save_as_obj_command(item))
                elif item.text(0).endswith(".dds"):
                    export_png_action = sub_menu.addAction("Save as .png")
                    export_png_action.triggered.connect(lambda: self._save_as_png(self._save_file_command(item)))
                
                menu.addAction(f"{item.filename}").setEnabled(False)
            
            menu.installEventFilter(ContextMenuFilter(menu))
            menu.popup(self.viewport().mapToGlobal(point))

    def _calculate_file_hash(self, file_path):
        """计算文件的 MD5 哈希值"""
        try:
            hash_md5 = hashlib.md5()
            with open(file_path, "rb") as f:
                for chunk in iter(lambda: f.read(4096), b""):
                    hash_md5.update(chunk)
            return hash_md5.hexdigest()
        except Exception as e:
            self.event_logger.add(f"Error calculating file hash: {str(e)}")
            return None
    
    def _extract_hash_from_resfile_hash(self, resfile_hash):
        """从 resfile_hash (格式: folder/hash) 中提取哈希值部分
        
        resfile_hash 格式通常是 "folder/hash"，其中：
        - hash 部分可能是 "prefix_hash" 格式（如 "cdeabdd06e0dedaa_6fa910a9ccb6af0d5de4b5a95119eac7"）
        - 实际的 MD5 哈希是下划线后的32个十六进制字符
        - 或者 hash 部分本身就是完整的哈希值
        """
        try:
            parts = resfile_hash.split("/", 1)
            if len(parts) == 2:
                hash_part = parts[1]
                # 如果包含下划线，提取最后一个下划线后的部分（通常是32个字符的MD5哈希）
                if "_" in hash_part:
                    hash_value = hash_part.split("_")[-1]
                    # MD5 哈希是32个十六进制字符
                    if len(hash_value) == 32:
                        return hash_value
                    # 如果下划线后的部分不是32个字符，尝试提取最后32个字符
                    # 去掉所有下划线和连字符，取最后32个字符
                    clean_hash = hash_part.replace("_", "").replace("-", "")
                    if len(clean_hash) >= 32:
                        return clean_hash[-32:]
                else:
                    # 没有下划线，可能是完整的哈希值
                    # 如果长度是32，直接使用；否则尝试提取最后32个字符
                    if len(hash_part) == 32:
                        return hash_part
                    elif len(hash_part) > 32:
                        return hash_part[-32:]
                    else:
                        return hash_part
            # 如果没有 "/"，可能是直接的哈希值
            return resfile_hash if len(resfile_hash) == 32 else resfile_hash[-32:] if len(resfile_hash) > 32 else resfile_hash
        except Exception as e:
            self.event_logger.add(f"Error extracting hash from resfile_hash: {str(e)}")
            return None
    
    def _verify_file_hash(self, file_path, expected_hash):
        """验证文件的哈希值是否与期望值一致"""
        if not expected_hash:
            return False
        
        actual_hash = self._calculate_file_hash(file_path)
        if not actual_hash:
            return False
        
        # 提取期望的哈希值（去掉可能的文件夹前缀）
        expected_hash_clean = self._extract_hash_from_resfile_hash(expected_hash)
        if not expected_hash_clean:
            return False
        
        # 比对哈希值（不区分大小写）
        return actual_hash.lower() == expected_hash_clean.lower()
    
    def _download_file_from_respath(self, respath, dest_path):
        """从 res 路径下载文件，优先使用本地缓存（验证哈希），如果不存在或哈希不匹配则从网络下载"""
        if not self.res_tree or not self.res_tree.resfiles_list:
            return False
        
        # 从 resfiles_list 中查找对应的文件
        respath_normalized = respath.lower()
        if respath_normalized.startswith("res:/"):
            respath_normalized = respath_normalized[5:]  # 去掉 "res:/"
        
        for resfile in self.res_tree.resfiles_list:
            res_path = resfile.get("res_path", "").lower()
            if res_path == respath_normalized:
                resfile_hash = resfile.get("resfile_hash", "")
                if resfile_hash:
                    # 优先尝试从本地 SharedCache 复制（并验证哈希）
                    try:
                        self.config = json.loads(open(CONFIG_FILE, "r", encoding="utf-8").read())
                        shared_cache_location = self.config.get("SharedCacheLocation", "")
                        if shared_cache_location:
                            folder, hash_part = resfile_hash.split("/", 1)
                            local_file_path = os.path.join(
                                shared_cache_location, "ResFiles", folder, hash_part
                            )
                            if os.path.exists(local_file_path):
                                # 验证文件哈希是否与索引中记录的哈希一致
                                if self._verify_file_hash(local_file_path, resfile_hash):
                                    # 哈希验证通过，从本地复制
                                    shutil.copy(local_file_path, dest_path)
                                    self.event_logger.add(f"{local_file_path} -> {dest_path}")
                                    return True
                                else:
                                    # 哈希不匹配，记录警告并从网络下载
                                    self.event_logger.add(f"Local file hash mismatch for {respath}, downloading from network...")
                    
                    except Exception as e:
                        # 本地缓存读取失败，继续尝试网络下载
                        pass
                    
                    # 如果本地不存在或哈希不匹配，从在线下载
                    resindex = ResFileIndex(
                        chinese_client=False, event_logger=self.event_logger
                    )
                    try:
                        url = f"{resindex.resources_url}/{resfile_hash}"
                        response = requests.get(url, timeout=30)
                        self.event_logger.add(f"Downloading file: {url} | Response: {response.status_code} | File: {respath}")
                        if response.status_code == 200:
                            with open(dest_path, "wb") as f:
                                f.write(response.content)
                            self.event_logger.add(f"{url} -> {dest_path}")
                            return True
                        else:
                            self.event_logger.add(f"Failed to download {respath}: HTTP {response.status_code} | URL: {url}")
                            return False
                    except Exception as e:
                        self.event_logger.add(f"Failed to download {respath}: {str(e)} | URL: {url}")
                        return False
        
        self.event_logger.add(f"File not found in resfileindex: {respath}")
        return False
    
    def _save_file_command(self, item, multiple=False, multiple_destination=None, convert_dds=False, convert_gr2=False):
        if not hasattr(item, 'respath'):
            return None
        
        if not multiple:
            dest_location, _ = QFileDialog.getSaveFileName(
                None, "Save File", item.text(0), "All Files(*.)"
            )
            if not dest_location:
                return None
            out_file_path = dest_location
        else:
            out_file_path = multiple_destination
        
        # 尝试从在线下载
        success = self._download_file_from_respath(item.respath, out_file_path)
        
        if success:
            if convert_dds and out_file_path.lower().endswith(".dds"):
                self._save_as_png(out_file_path)
            if convert_gr2 and out_file_path.lower().endswith(".gr2"):
                self._save_as_obj(out_file_path)
            if not multiple:
                self.event_logger.add(f"Exported file to: {out_file_path}")
            return out_file_path
        else:
            if not multiple:
                self.event_logger.add(f"Failed to export file: {item.respath}")
            return None

    def _save_as_png(self, out_file_path):
        """将 DDS 文件转换为 PNG"""
        NvttExport().run(out_file_path)
        os.remove(out_file_path)
    
    def _save_as_obj(self, out_file_path):
        """将 GR2 文件转换为 OBJ"""
        Wavefront().to_obj(out_file_path)
    
    def _save_as_obj_command(self, item):
        out_file = self._save_file_command(item)
        if not out_file:
            return
        Wavefront().to_obj(out_file)
        self.event_logger.add(f"Obj exported: {out_file}")

    def _get_unique_filename(self, dest_folder, filename):
        """生成唯一的文件名，如果重名则添加数字后缀"""
        base_path = os.path.join(dest_folder, filename)
        
        # 如果文件不存在，直接返回
        if not os.path.exists(base_path):
            return base_path
        
        # 如果文件存在，添加数字后缀
        name, ext = os.path.splitext(filename)
        counter = 1
        while True:
            new_filename = f"{name}_{counter}{ext}"
            new_path = os.path.join(dest_folder, new_filename)
            if not os.path.exists(new_path):
                return new_path
            counter += 1
    
    def _save_folder_command(self, item, convert_dds=False, convert_gr2=False):
        dest_folder = QFileDialog.getExistingDirectory(None, "Select Destination")
        if not dest_folder:
            return
        
        # 收集所有文件，不保持目录结构
        files = []
        def collect_files(parent_item):
            for i in range(parent_item.childCount()):
                child = parent_item.child(i)
                if isinstance(child, EVEFile):
                    files.append(child)
                elif isinstance(child, EVEDirectory):
                    collect_files(child)
        
        collect_files(item)
        
        if not files:
            return
        
        # 为每个文件生成唯一的文件名（处理重名问题）
        file_destinations = []
        used_filenames = set()
        
        for file_item in files:
            if hasattr(file_item, 'respath'):
                # 从 respath 中提取实际文件名（保持原始扩展名）
                # respath 格式：res:/dx9/model/ship/amarr/frigate/af1/texture.dds
                respath = file_item.respath
                if respath.startswith("res:/"):
                    respath = respath[5:]  # 去掉 "res:/" 前缀
                original_filename = os.path.basename(respath)  # 获取实际文件名，如 texture.dds
                
                # 生成唯一文件名（使用原始文件名，不提前修改扩展名）
                dest_path = self._get_unique_filename(dest_folder, original_filename)
                file_destinations.append((file_item, dest_path))
        
        if not file_destinations:
            return
        
        # 使用进度条显示下载进度
        loading = LoadingScreenWindow(file_destinations, stay_on_top=True)
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as worker:
            futures = []
            
            for file_item, dest_path in file_destinations:
                # 确保目标目录存在（虽然所有文件都在同一目录，但为了安全）
                os.makedirs(os.path.dirname(dest_path) if os.path.dirname(dest_path) else dest_folder, exist_ok=True)
                futures.append(
                    worker.submit(self._save_file_command, file_item, True, dest_path, convert_dds, convert_gr2)
                )
            
            for future in concurrent.futures.as_completed(futures):
                result = future.result()
                if result:
                    loading.label.setText(os.path.basename(result))
                loading.setValue(loading.value() + 1)
                QApplication.processEvents()
        
        self.event_logger.add(f"Exported {len(file_destinations)} files to {dest_folder}")
        loading.close()
    
    def _is_filtered_file(self, dep_path, icon_folder=None, sof_race_name=None):
        """
        判断文件是否应该被过滤（即是否应该放在根目录）
        
        参数:
        - dep_path: 依赖文件路径，如 res:/dx9/model/ship/amarr/frigate/af1/af1_t1.gr2
        - icon_folder: 图标文件夹路径，如 res:/dx9/model/ship/amarr/titan/at1/icons
        - sof_race_name: 种族名称，如 amarr
        
        返回:
        - bool: True 表示应该被过滤（放在根目录），False 表示未过滤（放在 others 目录）
        """
        dep_lower = dep_path.lower()
        
        # 模型文件（.gr2）：所有 .gr2 文件都算过滤后的（放在根目录）
        if dep_lower.endswith('.gr2'):
            return True
        
        # 贴图文件（.dds, .png, .jpg）：应用过滤策略
        if dep_lower.endswith('.dds') or dep_lower.endswith('.png') or dep_lower.endswith('.jpg'):
            # 过滤掉包含 _lowdetail 或 _mediumdetail 的文件
            file_name = os.path.basename(dep_path)
            if "_lowdetail" in file_name or "_mediumdetail" in file_name:
                return False  # 未过滤，放在 others 目录
            
            # 对于 DDS 文件，应用额外的路径过滤条件
            if dep_lower.endswith('.dds'):
                # 从 iconFolder 推导飞船根目录
                ship_root_directory = None
                if icon_folder:
                    if icon_folder.startswith("res:/"):
                        base_path = icon_folder[5:]  # 去掉 "res:/"
                    else:
                        base_path = icon_folder
                    
                    # 去掉末尾的 /icons 或 icons
                    if base_path.endswith("/icons"):
                        ship_root_directory = base_path[:-6]  # 去掉 "/icons"
                    elif base_path.endswith("icons"):
                        ship_root_directory = base_path[:-5]  # 去掉 "icons"
                    
                    # 确保路径格式正确
                    if ship_root_directory:
                        if not ship_root_directory.startswith("res:/"):
                            ship_root_directory = f"res:/{ship_root_directory}"
                        if not ship_root_directory.endswith("/"):
                            ship_root_directory += "/"
                
                # 从种族名称推导共享目录
                shared_texture_directory = None
                if sof_race_name:
                    race_name_lower = sof_race_name.lower()
                    shared_texture_directory = f"res:/dx9/model/shared/{race_name_lower}/textures/"
                
                # 检查 DDS 文件是否满足路径条件
                is_valid = False
                
                # 条件1：贴图位于飞船根目录及其子目录下
                if ship_root_directory:
                    ship_root_lower = ship_root_directory.lower()
                    if dep_lower.startswith(ship_root_lower):
                        is_valid = True
                
                # 条件2：贴图位于此飞船种族的共享目录
                if not is_valid and shared_texture_directory:
                    shared_dir_lower = shared_texture_directory.lower()
                    if dep_lower.startswith(shared_dir_lower):
                        is_valid = True
                
                # 如果 DDS 文件不符合任一条件，放在 others 目录
                return is_valid
            
            # PNG 和 JPG 文件不需要路径过滤，只需要通过 _lowdetail 和 _mediumdetail 过滤
            # 如果通过了上面的过滤，就放在根目录
            return True
        
        # 其他类型的文件：放在 others 目录
        return False
    
    def _export_all_dependencies_command(self, item, convert_dds=False, convert_gr2=False):
        """导出飞船的所有依赖文件，应用过滤逻辑：过滤后的文件放在根目录，未过滤的文件放在 others 目录"""
        # 检查是否是飞船目录
        if not hasattr(item, 'graphics_info') or not item.graphics_info:
            self.event_logger.add("Error: This item does not have ship information")
            return
        
        # 获取 sof_hull_name
        graphics_info = item.graphics_info
        sof_hull_name = graphics_info.get('sofHullName', '')
        if not sof_hull_name:
            self.event_logger.add("Error: Cannot find sofHullName for this ship")
            return
        
        # 获取所有依赖
        all_dependencies = self._get_ship_dependencies(sof_hull_name)
        
        # 获取图形信息用于过滤判断和查找 faction 目录
        icon_folder = graphics_info.get('iconFolder', '')
        sof_race_name = graphics_info.get('sofRaceName', '')
        sof_faction_name = graphics_info.get('sofFactionName', '')
        
        # 获取飞船根目录
        ship_root_directory = None
        if icon_folder and sof_hull_name:
            ship_root_directory = self._find_ship_root_directory(icon_folder, sof_hull_name)
        
        # 获取 faction 目录中的文件（如果存在）
        faction_files = []
        if ship_root_directory and sof_faction_name:
            faction_files = self._find_faction_directory_files(ship_root_directory, sof_faction_name, sof_hull_name, icon_folder, sof_race_name)
        
        # 合并依赖文件和 faction 文件，去重
        all_files = list(set(all_dependencies + faction_files))
        
        if not all_files:
            self.event_logger.add(f"No dependencies or faction files found for ship: {sof_hull_name}")
            return
        
        # 选择目标文件夹
        dest_folder = QFileDialog.getExistingDirectory(None, "Select Destination")
        if not dest_folder:
            return
        
        # 分离过滤后的文件和未过滤的文件
        filtered_files = []  # 放在根目录的文件
        unfiltered_files = []  # 放在 others 目录的文件
        
        for dep_path in all_files:
            # 确保路径是完整的 res:/ 格式用于判断
            full_dep_path = dep_path if dep_path.startswith("res:/") else f"res:/{dep_path}"
            is_filtered = self._is_filtered_file(full_dep_path, icon_folder, sof_race_name)
            
            if is_filtered:
                filtered_files.append(dep_path)
            else:
                unfiltered_files.append(dep_path)
        
        self.event_logger.add(f"Exporting {len(all_files)} files for ship: {item.text(0)} (sofHullName: {sof_hull_name})")
        self.event_logger.add(f"  - {len(all_dependencies)} from dependencies")
        if faction_files:
            self.event_logger.add(f"  - {len(faction_files)} from faction directory")
        self.event_logger.add(f"  - {len(filtered_files)} filtered files (root directory)")
        self.event_logger.add(f"  - {len(unfiltered_files)} unfiltered files (others directory)")
        
        # 创建 others 目录
        others_folder = os.path.join(dest_folder, "others")
        os.makedirs(others_folder, exist_ok=True)
        
        # 为过滤后的文件生成目标路径（根目录）
        filtered_destinations = []
        for dep_path in filtered_files:
            # 从依赖路径中提取文件名
            if dep_path.startswith("res:/"):
                dep_path_clean = dep_path[5:]  # 去掉 "res:/" 前缀
            else:
                dep_path_clean = dep_path
            original_filename = os.path.basename(dep_path_clean)
            
            # 生成唯一文件名（根目录）
            dest_path = self._get_unique_filename(dest_folder, original_filename)
            filtered_destinations.append((dep_path, dest_path))
        
        # 为未过滤的文件生成目标路径（others 目录）
        unfiltered_destinations = []
        for dep_path in unfiltered_files:
            # 从依赖路径中提取文件名
            if dep_path.startswith("res:/"):
                dep_path_clean = dep_path[5:]  # 去掉 "res:/" 前缀
            else:
                dep_path_clean = dep_path
            original_filename = os.path.basename(dep_path_clean)
            
            # 生成唯一文件名（others 目录）
            dest_path = self._get_unique_filename(others_folder, original_filename)
            unfiltered_destinations.append((dep_path, dest_path))
        
        # 合并所有文件目标
        all_destinations = filtered_destinations + unfiltered_destinations
        
        if not all_destinations:
            return
        
        # 使用进度条显示下载进度
        loading = LoadingScreenWindow(all_destinations, stay_on_top=True)
        
        # 第一阶段：下载/复制所有文件
        files_to_process = []  # 收集 (dep_path, dest_path, is_dds, is_gr2)
        for dep_path, dest_path in all_destinations:
            original_filename = os.path.basename(dest_path)
            is_dds = convert_dds and original_filename.lower().endswith(".dds")
            is_gr2 = convert_gr2 and original_filename.lower().endswith(".gr2")
            files_to_process.append((dep_path, dest_path, is_dds, is_gr2))
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as worker:
            futures = []
            
            for dep_path, dest_path, is_dds, is_gr2 in files_to_process:
                # 确保目标目录存在
                os.makedirs(os.path.dirname(dest_path) if os.path.dirname(dest_path) else dest_folder, exist_ok=True)
                # 下载文件（不立即转换）
                futures.append(
                    worker.submit(self._download_dependency_file, dep_path, dest_path)
                )
            
            for future in concurrent.futures.as_completed(futures):
                result = future.result()
                if result:
                    loading.label.setText(os.path.basename(result))
                loading.setValue(loading.value() + 1)
                QApplication.processEvents()
        
        # 第二阶段：统一转换 DDS 和 GR2 文件
        if convert_dds or convert_gr2:
            loading.label.setText("Converting files...")
            loading.setMaximum(len(files_to_process))
            loading.setValue(0)
            
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as converter:
                convert_futures = []
                for dep_path, dest_path, is_dds, is_gr2 in files_to_process:
                    if os.path.exists(dest_path):  # 只转换成功下载的文件
                        if is_dds:
                            convert_futures.append(converter.submit(self._save_as_png, dest_path))
                        elif is_gr2:
                            convert_futures.append(converter.submit(self._save_as_obj, dest_path))
                
                for future in concurrent.futures.as_completed(convert_futures):
                    result = future.result()
                    loading.setValue(loading.value() + 1)
                    QApplication.processEvents()
        
        self.event_logger.add(f"Exported {len(all_destinations)} dependencies to {dest_folder} ({len(filtered_destinations)} in root, {len(unfiltered_destinations)} in others)")
        loading.close()
    
    def _download_dependency_file(self, dep_path, dest_path):
        """下载单个依赖文件"""
        try:
            # 尝试从本地缓存复制
            if self.res_tree and self.res_tree.resfiles_list:
                # 查找对应的 resfile_hash
                dep_path_normalized = dep_path.lower()
                if dep_path_normalized.startswith("res:/"):
                    dep_path_normalized = dep_path_normalized[5:]
                
                for resfile in self.res_tree.resfiles_list:
                    res_path = resfile.get("res_path", "").lower()
                    if res_path == dep_path_normalized:
                        resfile_hash = resfile.get("resfile_hash", "")
                        if resfile_hash:
                            # 尝试从本地 SharedCache 复制
                            try:
                                self.config = json.loads(open(CONFIG_FILE, "r", encoding="utf-8").read())
                                shared_cache_location = self.config.get("SharedCacheLocation", "")
                                if shared_cache_location:
                                    folder, hash_part = resfile_hash.split("/", 1)
                                    local_file_path = os.path.join(
                                        shared_cache_location, "ResFiles", folder, hash_part
                                    )
                                    if os.path.exists(local_file_path):
                                        # 验证文件哈希
                                        if self._verify_file_hash(local_file_path, resfile_hash):
                                            shutil.copy(local_file_path, dest_path)
                                            self.event_logger.add(f"{local_file_path} -> {dest_path}")
                                            return dest_path
                            except Exception:
                                pass
                            
                            # 如果本地不存在或哈希不匹配，从在线下载
                            resindex = ResFileIndex(
                                chinese_client=False, event_logger=self.event_logger
                            )
                            try:
                                url = f"{resindex.resources_url}/{resfile_hash}"
                                response = requests.get(url, timeout=30)
                                self.event_logger.add(f"Downloading: {url} | Response: {response.status_code}")
                                if response.status_code == 200:
                                    with open(dest_path, "wb") as f:
                                        f.write(response.content)
                                    self.event_logger.add(f"{url} -> {dest_path}")
                                    return dest_path
                            except Exception as e:
                                self.event_logger.add(f"Error downloading {dep_path}: {str(e)}")
            return None
        except Exception as e:
            self.event_logger.add(f"Error downloading dependency {dep_path}: {str(e)}")
            return None


class CynoExporterWindow(QMainWindow):
    def __init__(self):
        try:
            super().__init__()
            self.setFixedSize(900, 900)
            self.setWindowTitle(WINDOW_TITLE)
            self.setWindowIcon(QIcon("icon.ico"))
            self.init()
        except Exception as e:
            print(f"错误发生在 CynoExporterWindow.__init__: {e}", file=sys.stderr, flush=True)
            import traceback
            traceback.print_exc(file=sys.stderr)
            raise

    def init(self):

        self.move(
            QApplication.primaryScreen().geometry().center() - self.rect().center()
        )

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)

        self.setStyleSheet(STYLE_SHEET)
        self.event_logger = EventLogger()

        self.set_shared_cache_action = QAction("&Set Shared Cache", self)
        self.set_shared_cache_action.triggered.connect(self.set_shared_cache)
        self.set_shared_cache_action.setEnabled(False)

        # 创建标签页
        self.tab_widget = QTabWidget()
        
        self.ship_tree = ShipTree(
            self,
            event_logger=self.event_logger,
        )
        
        self.shared_cache = ResTree(
            self,
            event_logger=self.event_logger,
            shared_cache=self.set_shared_cache_action,
            ship_tree=self.ship_tree,  # 传递 ShipTree 引用
        )
        
        # 设置 ShipTree 的 ResTree 引用，用于查找模型文件
        self.ship_tree.res_tree = self.shared_cache
        
        # 设置 Local 加载完成后的回调函数
        self.shared_cache.on_load_complete_callback = lambda: self.tab_widget.setTabEnabled(1, True)

        self.tab_widget.addTab(self.shared_cache, "Local")
        self.tab_widget.addTab(self.ship_tree, "Ship")
        
        # 初始化时禁用 Ship 标签页，直到 Local 加载完成
        self.tab_widget.setTabEnabled(1, False)

        self.menu_bar = QMenuBar()
        self.setMenuBar(self.menu_bar)

        self.help_menu = QMenu("&Help", self)
        self.menu_bar.addMenu(self.help_menu)

        help_action = QAction("&About", self)
        help_action.triggered.connect(lambda: AboutDialogPanel(self))
        logs_action = QAction("&Logs", self)
        logs_action.triggered.connect(lambda: LogsDialogPanel(self, self.event_logger))

        self.help_menu.addAction(self.set_shared_cache_action)
        self.help_menu.addSeparator()
        self.help_menu.addAction(help_action)
        self.help_menu.addAction(logs_action)

        self.tab_widget.currentChanged.connect(self.on_tab_change)

        self.text_box = QLineEdit()
        self.text_box.setPlaceholderText("Search... ex: af3_t1.gr2, Punisher")
        self.text_box.textChanged.connect(self._search)
        self.text_box.returnPressed.connect(self._next_search_item)
        self.search_results = []
        self.search_index = -1

        self.search_label = QLabel("")
        self.search_label.setStyleSheet("font-weight: bold;")

        search_shortcut = QShortcut(QKeySequence("Ctrl+F"), self)
        search_shortcut.activated.connect(self._search_shortcut)

        main_layout.addWidget(self.tab_widget)
        main_layout.addWidget(self.text_box)
        main_layout.addWidget(self.search_label)

    def _search_shortcut(self):
        self.text_box.setFocus()

    def _get_searches(self, item, search_str):
        results = []
        if isinstance(item, EVEFile) and (
            search_str in item.filename.lower() or search_str in item.text(0).lower()
        ):
            results.append(item)

        for i in range(item.childCount()):
            results.extend(self._get_searches(item.child(i), search_str))

        return results

    def _search(self, search_str):
        tree = self.tab_widget.currentWidget()
        self.search_label.setText("")
        if not isinstance(tree, (ResTree, ShipTree)):
            return

        self.search_results.clear()
        self.search_index = -1

        if not search_str:
            tree.collapseAll()
            return

        root = tree.topLevelItem(0)
        if root:
            self.search_results = self._get_searches(root, search_str.lower())

        if self.search_results:
            self.search_index = 0
            self._select_search_item(tree)
        else:
            tree.clearSelection()
            self.search_index = -1
            self.search_label.setText("")

    def _next_search_item(self):
        tree = self.tab_widget.currentWidget()
        if not self.search_results:
            self._search(self.text_box.text())
            return
        self.search_index = (self.search_index + 1) % len(self.search_results)
        self._select_search_item(tree)

    def _select_search_item(self, tree):
        item = self.search_results[self.search_index]
        parent = item.parent()
        while parent:
            tree.expandItem(parent)
            parent = parent.parent()
        tree.setCurrentItem(item)
        tree.scrollToItem(item, QTreeWidget.ScrollHint.PositionAtCenter)
        self.search_label.setText(
            f"{self.search_index + 1} of {len(self.search_results)}"
        )

    def set_shared_cache(self):
        folder = QFileDialog.getExistingDirectory(
            None, "Path to EVE's SharedCache folder"
        )
        if not folder:
            return
        with open("./config.json", "w", encoding="utf-8") as f:
            json.dump({"SharedCacheLocation": folder}, f, indent=4)

        self.shared_cache.are_resfiles_loaded = False
        self.shared_cache.load_resfiles(
            self.shared_cache, self.shared_cache.client
        )

    def on_tab_change(self, index):
        """标签页切换时的处理"""
        current_tab = self.tab_widget.tabText(index)
        
        # 如果尝试切换到 Ship 标签页，但 Local 还未加载完成，阻止切换
        if current_tab == "Ship" and not self.shared_cache.are_resfiles_loaded:
            self.event_logger.add("Cannot switch to Ship tab: Local files are still loading. Please wait...")
            # 切换回 Local 标签页
            self.tab_widget.setCurrentIndex(0)
            return
        
        self.event_logger.add(f"Switching to tab: {current_tab}")
        
        # 如果 Ship 标签页已启用但数据未加载，且 SDE 下载已完成，则加载飞船数据
        if current_tab == "Ship" and not self.ship_tree.are_ships_loaded:
            # 检查 SDE 是否已下载完成
            if self.ship_tree.sde_path:
                # SDE 已下载，直接加载飞船数据
                self.tab_widget.tabBar().setEnabled(False)
                try:
                    root = self.ship_tree.topLevelItem(0)
                    if root:
                        self.ship_tree._load_ship_files(root)
                        self.ship_tree.are_ships_loaded = True
                except Exception as e:
                    import traceback
                    error_msg = f"Error loading ship files: {str(e)}\n{traceback.format_exc()}"
                    self.event_logger.add(error_msg)
                self.tab_widget.tabBar().setEnabled(True)
            elif self.ship_tree.download_thread and self.ship_tree.download_thread.isRunning():
                # SDE 正在下载中，等待完成
                self.event_logger.add("SDE download in progress. Please wait...")
            else:
                # SDE 下载未启动，启动下载
                self.tab_widget.tabBar().setEnabled(False)
                self.ship_tree.load_ships()
                self.tab_widget.tabBar().setEnabled(True)

    def closeEvent(self, event):
        # i do this because if you exit while its still loading resfiles
        # the app will persist due to how the loading widget operates
        os.system('taskkill /F /IM "Cyno Exporter.exe"')


class DialogPanel(QDialog):
    def __init__(self, parent, title):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setWindowFlags(
            self.windowFlags() & ~Qt.WindowType.WindowContextHelpButtonHint
        )
        self.setWindowModality(Qt.WindowModality.ApplicationModal)


class EventLogger(QObject):
    on_update = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.log_items = []

    def add(self, message):
        self.log_items.append(
            {"time": datetime.now().strftime("%H:%M:%S"), "message": message}
        )
        self.on_update.emit()


class LogsDialogPanel(DialogPanel):
    def __init__(self, parent, event_logger):
        super().__init__(parent, "Logs")

        self.logs_widget = QTreeWidget(self)
        self.logs_widget.setHeaderLabels(["Time", "Event"])
        self.logs_widget.header().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)

        self.setMinimumWidth(400)
        self.logs_widget.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )

        layout = QVBoxLayout(self)
        layout.addWidget(self.logs_widget, stretch=1)

        self.event_logger = event_logger
        self.event_logger.on_update.connect(self._update)
        self._update()

        self.setWindowModality(Qt.WindowModality.NonModal)
        self.show()

    def _update(self):
        self.logs_widget.clear()
        for log in self.event_logger.log_items:
            item = QTreeWidgetItem(self.logs_widget)
            item.setText(0, log["time"])
            item.setText(1, log["message"])


class LicenseAgreementDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)

        from utils.license_agreement import LICENSE_TEXT

        self.setWindowTitle("License Agreement")
        self.setFixedSize(900, 525)

        legal_disclaimer = QTextEdit(self)
        legal_disclaimer.setPlainText(LICENSE_TEXT)
        legal_disclaimer.setStyleSheet("font-size: 11px;")
        legal_disclaimer.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )

        accept_button = QPushButton("I accept", self)
        accept_button.clicked.connect(self.accept)

        layout = QVBoxLayout(self)
        layout.addWidget(legal_disclaimer, stretch=1)
        layout.addWidget(accept_button)


class AboutDialogPanel(DialogPanel):
    def __init__(self, parent):
        super().__init__(parent, "About")

        app_label = QLabel(f"<img src='icon.ico', width='40'> {WINDOW_TITLE}", self)
        app_label.setStyleSheet("font-size: 24px;")

        self.setFixedSize(700, 525)

        author = QLabel("Author", self)
        author.setStyleSheet("font-size: 15px; font-weight: bold;")

        label = QLabel(
            'Tyloth: <a style="color:white;" href="https://github.com/largeBIGsnooze/cyno-exporter">https://github.com/largeBIGsnooze/cyno-exporter</a>',
            self,
        )
        label.setStyleSheet("font-size: 12px;")
        label.setOpenExternalLinks(True)

        credit = QLabel("Credits", self)
        credit.setStyleSheet("font-size: 15px; font-weight: bold;")

        credit_link = QLabel(
            'leixingyu, unrealStylesheet: <a style="color:white;" href="https://github.com/leixingyu/unrealStylesheet">https://github.com/leixingyu/unrealStylesheet</a>',
            self,
        )
        credit_text = QLabel(
            'Khossyy, ww2ogg: <a style="color: white;" href="https://github.com/khossyy/wem2ogg">https://github.com/khossyy/wem2ogg</a>',
            self,
        )
        credit_text_2 = QLabel(
            'Tamber, gr2tojson: <a style="color: white;" href="https://github.com/cppctamber/evegr2tojson">https://github.com/cppctamber/evegr2tojson</a>',
            self,
        )
        credit_text_3 = QLabel(
            'ItsBranK, ReVorb: <a style="color: white;" href="https://github.com/ItsBranK/ReVorb">https://github.com/ItsBranK/ReVorb</a>',
            self,
        )
        credit_link.setOpenExternalLinks(True)
        credit_text.setOpenExternalLinks(True)
        credit_text_2.setOpenExternalLinks(True)
        credit_text_3.setOpenExternalLinks(True)

        legal_disclaimner_header = QLabel("Legal Disclaimer", self)
        legal_disclaimner_header.setStyleSheet("font-size: 15px; font-weight: bold;")
        legal_disclaimer = QTextEdit(self)
        legal_disclaimer.setPlainText(
            "This tool provides access to materials used with limited permission of CCP Games.It is not endorsed by CCP Games and does not reflect the views or opinions of CCP Games or anyone officially involved in producing or managing EVE Online.\n\nAs such, it does not contribute to the official narrative of the fictional universe, if applicable.\n\nEVE Online © CCP Games.\n\nTHE SOFTWARE IS PROVIDED AS IS, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.\n\nIN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE."
        )
        legal_disclaimer.setStyleSheet("font-size: 11px;")

        legal_disclaimer.setReadOnly(True)
        legal_disclaimer.setFixedWidth(700)

        div = QWidget(self)
        div.setStyleSheet("margin: 5px;")
        div.setFixedWidth(700)

        layout = QVBoxLayout(div)
        layout.addWidget(app_label)
        layout.addWidget(author)
        layout.addWidget(label)
        layout.addWidget(credit)
        layout.addWidget(credit_link)
        layout.addWidget(credit_text)
        layout.addWidget(credit_text_2)
        layout.addWidget(credit_text_3)
        layout.addWidget(legal_disclaimner_header)
        layout.addWidget(legal_disclaimer)

        self.show()


class ProgressBar(QProgressBar):
    def __init__(self, files, parent):
        super().__init__(parent)
        self.setGeometry(0, 770, 900, 15)
        self.setStyleSheet("border-top: 5px solid #242424;")
        self.setValue(0)
        self.setMaximum(len(files))
        self.show()


class LoadingScreenWindow(QProgressDialog):
    def __init__(self, files, stay_on_top=False):
        super().__init__()
        self.setLabelText("Loading...")
        self.setWindowTitle(WINDOW_TITLE)
        self.setCancelButton(None)
        self.setValue(0)

        self.label = QLabel("", self)
        self.label.setObjectName("loadingLabel")
        self.setLabel(self.label)

        self.setMaximum(len(files))
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.FramelessWindowHint)
        self.setFixedSize(300, 60)
        self.move(
            QApplication.primaryScreen().geometry().center() - self.rect().center()
        )
        self.setStyleSheet(STYLE_SHEET)
        self.setWindowModality(
            Qt.WindowModality.WindowModal
            if not stay_on_top
            else Qt.WindowModality.ApplicationModal
        )
        self.show()
        self.raise_()


def exception_hook(exc_type, exc_value, exc_traceback):
    """全局异常处理函数，捕获所有未处理的异常并输出到终端"""
    import traceback
    
    # 忽略 KeyboardInterrupt，让程序正常退出
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    
    error_msg = "=" * 80 + "\n"
    error_msg += "未捕获的异常发生！\n"
    error_msg += "=" * 80 + "\n"
    error_msg += f"异常类型: {exc_type.__name__}\n"
    error_msg += f"异常信息: {str(exc_value)}\n"
    error_msg += "=" * 80 + "\n"
    error_msg += "完整堆栈跟踪:\n"
    error_msg += "=" * 80 + "\n"
    error_msg += "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
    error_msg += "=" * 80 + "\n"
    
    # 输出到 stderr（终端）- 确保在 Windows 控制台也能显示
    try:
        print(error_msg, file=sys.stderr, flush=True)
        # 同时输出到 stdout，以防 stderr 被重定向
        print(error_msg, flush=True)
    except:
        # 如果输出失败，尝试写入文件
        try:
            with open("error_log.txt", "a", encoding="utf-8") as f:
                f.write(error_msg)
                f.write(f"\n时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        except:
            pass
    
    # 如果是 PyQt 应用，尝试显示错误对话框
    try:
        app = QApplication.instance()
        if app is not None:
            msg_box = QMessageBox()
            msg_box.setIcon(QMessageBox.Icon.Critical)
            msg_box.setWindowTitle("程序错误")
            msg_box.setText(f"发生未处理的异常:\n\n{exc_type.__name__}: {str(exc_value)}\n\n详细信息已输出到终端。")
            msg_box.setDetailedText("".join(traceback.format_exception(exc_type, exc_value, exc_traceback)))
            msg_box.exec()
    except:
        pass  # 如果无法显示对话框，至少已经输出到终端了


if __name__ == "__main__":
    # 设置全局异常处理
    sys.excepthook = exception_hook
    
    parser = argparse.ArgumentParser()
    parser.add_argument("--dev", action="store_true")
    args = parser.parse_args()

    try:
        app = QApplication(sys.argv)
        app.setWindowIcon(QIcon("icon.ico"))
        window = CynoExporterWindow()
        license_agreement = LicenseAgreementDialog()
        license_agreement.setStyleSheet(STYLE_SHEET)

        def show():
            try:
                window.show()
                window.shared_cache.load_resfiles(
                    window.shared_cache, window.shared_cache.client
                )
                sys.exit(app.exec())
            except Exception as e:
                print(f"错误发生在 show() 函数中: {e}", file=sys.stderr, flush=True)
                import traceback
                traceback.print_exc(file=sys.stderr)
                raise

        if not args.dev:
            if license_agreement.exec() == QDialog.DialogCode.Accepted:
                show()
        else:
            show()
    except Exception as e:
        print(f"错误发生在程序初始化阶段: {e}", file=sys.stderr, flush=True)
        import traceback
        traceback.print_exc(file=sys.stderr)
        raise
