import base64
import datetime
import os
import platform
import shutil
import sys
import sysconfig
import typing
import warnings
import zipfile
import urllib.request
import io
import json
import threading
import subprocess

from collections.abc import Iterable
from hashlib import sha3_512
from pathlib import Path


# This is a bit jank. We need cx-Freeze to be able to run anything from this script, so install it
try:
    requirement = 'cx-Freeze>=6.15.10'
    import pkg_resources
    try:
        pkg_resources.require(requirement)
        install_cx_freeze = False
    except pkg_resources.ResolutionError:
        install_cx_freeze = True
except ImportError:
    install_cx_freeze = True
    pkg_resources = None  # type: ignore [assignment]

if install_cx_freeze:
    # check if pip is available
    try:
        import pip  # noqa: F401
    except ImportError:
        raise RuntimeError("pip not available. Please install pip.")
    # install and import cx_freeze
    if '--yes' not in sys.argv and '-y' not in sys.argv:
        input(f'Requirement {requirement} is not satisfied, press enter to install it')
    subprocess.call([sys.executable, '-m', 'pip', 'install', requirement, '--upgrade'])
    import pkg_resources

import cx_Freeze

# .build only exists if cx-Freeze is the right version, so we have to update/install that first before this line
import setuptools.command.build

if __name__ == "__main__":
    pass
    # need to run this early to import from Utils and Launcher
    # TODO: move stuff to not require this
    #import ModuleUpdate
    #ModuleUpdate.update(yes="--yes" in sys.argv or "-y" in sys.argv)

from Utils import version_tuple, is_windows, is_linux
from Cython.Build import cythonize


signtool: typing.Optional[str]
if os.path.exists("X:/pw.txt"):
    print("Using signtool")
    with open("X:/pw.txt", encoding="utf-8-sig") as f:
        pw = f.read()
    signtool = r'signtool sign /f X:/_SITS_Zertifikat_.pfx /p "' + pw + \
               r'" /fd sha256 /tr http://timestamp.digicert.com/ '
else:
    signtool = None


build_platform = sysconfig.get_platform()
arch_folder = "exe.{platform}-{version}".format(platform=build_platform,
                                                version=sysconfig.get_python_version())
buildfolder = Path("build", arch_folder)
build_arch = build_platform.split('-')[-1] if '-' in build_platform else platform.machine()


exes = [
    cx_Freeze.Executable(
        script=f"stardew_tracker.py",
        target_name="stardew_tracker" + (".exe" if is_windows else ""),
        base=None
    )
]

extra_data = ["LICENSE"]
extra_libs = ["libssl.so", "libcrypto.so"] if is_linux else []


def _threaded_hash(filepath):
    hasher = sha3_512()
    hasher.update(open(filepath, "rb").read())
    return base64.b85encode(hasher.digest()).decode()


# cx_Freeze's build command runs other commands. Override to accept --yes and store that.
class BuildCommand(setuptools.command.build.build):
    user_options = [
        ('yes', 'y', 'Answer "yes" to all questions.'),
    ]
    yes: bool
    last_yes: bool = False  # used by sub commands of build

    def initialize_options(self):
        super().initialize_options()
        type(self).last_yes = self.yes = False

    def finalize_options(self):
        super().finalize_options()
        type(self).last_yes = self.yes


# Override cx_Freeze's build_exe command for pre and post build steps
class BuildExeCommand(cx_Freeze.command.build_exe.build_exe):
    user_options = cx_Freeze.command.build_exe.build_exe.user_options + [
        ('yes', 'y', 'Answer "yes" to all questions.'),
        ('extra-data=', None, 'Additional files to add.'),
    ]
    yes: bool
    extra_data: Iterable  # [any] not available in 3.8
    extra_libs: Iterable  # work around broken include_files

    buildfolder: Path
    libfolder: Path
    library: Path
    buildtime: datetime.datetime

    def initialize_options(self):
        super().initialize_options()
        self.yes = BuildCommand.last_yes
        self.extra_data = []
        self.extra_libs = []

    def finalize_options(self):
        super().finalize_options()
        self.buildfolder = self.build_exe
        self.libfolder = Path(self.buildfolder, "lib")
        self.library = Path(self.libfolder, "library.zip")

    def installfile(self, path, subpath=None, keep_content: bool = False):
        folder = self.buildfolder
        if subpath:
            folder /= subpath
        print('copying', path, '->', folder)
        if path.is_dir():
            folder /= path.name
            if folder.is_dir() and not keep_content:
                shutil.rmtree(folder)
            shutil.copytree(path, folder, dirs_exist_ok=True)
        elif path.is_file():
            shutil.copy(path, folder)
        else:
            print('Warning,', path, 'not found')

    def create_manifest(self, create_hashes=False):
        # Since the setup is now split into components and the manifest is not,
        # it makes most sense to just remove the hashes for now. Not aware of anyone using them.
        hashes = {}
        manifestpath = os.path.join(self.buildfolder, "manifest.json")
        if create_hashes:
            from concurrent.futures import ThreadPoolExecutor
            pool = ThreadPoolExecutor()
            for dirpath, dirnames, filenames in os.walk(self.buildfolder):
                for filename in filenames:
                    path = os.path.join(dirpath, filename)
                    hashes[os.path.relpath(path, start=self.buildfolder)] = pool.submit(_threaded_hash, path)

        import json
        manifest = {
            "buildtime": self.buildtime.isoformat(sep=" ", timespec="seconds"),
            "hashes": {path: hash.result() for path, hash in hashes.items()},
            "version": version_tuple}

        json.dump(manifest, open(manifestpath, "wt"), indent=4)
        print("Created Manifest")

    def run(self):
        # pre-build steps
        print(f"Outputting to: {self.buildfolder}")
        os.makedirs(self.buildfolder, exist_ok=True)

        # auto-build cython modules
        build_ext = self.distribution.get_command_obj("build_ext")
        build_ext.inplace = False
        self.run_command("build_ext")
        # find remains of previous in-place builds, try to delete and warn otherwise
        for path in build_ext.get_outputs():
            parts = os.path.split(path)[-1].split(".")
            pattern = parts[0] + ".*." + parts[-1]
            for match in Path().glob(pattern):
                try:
                    match.unlink()
                    print(f"Removed {match}")
                except Exception as ex:
                    warnings.warn(f"Could not delete old build output: {match}\n"
                                  f"{ex}\nPlease close all AP instances and delete manually.")

        # regular cx build
        self.buildtime = datetime.datetime.utcnow()
        super().run()

        # manually copy built modules to lib folder. cx_Freeze does not know they exist.
        for src in build_ext.get_outputs():
            print(f"copying {src} -> {self.libfolder}")
            shutil.copy(src, self.libfolder, follow_symlinks=False)

        # include_files seems to not be done automatically. implement here
        for src, dst in self.include_files:
            print(f"copying {src} -> {self.buildfolder / dst}")
            shutil.copyfile(src, self.buildfolder / dst, follow_symlinks=False)

        # now that include_files is completely broken, run find_libs here
        for src, dst in find_libs(*self.extra_libs):
            print(f"copying {src} -> {self.buildfolder / dst}")
            shutil.copyfile(src, self.buildfolder / dst, follow_symlinks=False)

        # post build steps
        if is_windows:
            if Path("VC_redist.x64.exe").is_file():
                print("VC Redist found, skipping download")
            else:
                # windows needs Visual Studio C++ Redistributable
                # Installer works for x64 and arm64
                print("Downloading VC Redist")
                import certifi
                import ssl
                context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=certifi.where())
                with urllib.request.urlopen(r"https://aka.ms/vs/17/release/vc_redist.x64.exe",
                                            context=context) as download:
                    vc_redist = download.read()
                print(f"Download complete, {len(vc_redist) / 1024 / 1024:.2f} MBytes downloaded.", )
                with open("VC_redist.x64.exe", "wb") as vc_file:
                    vc_file.write(vc_redist)

        for data in self.extra_data:
            self.installfile(Path(data))

        self.create_manifest()

        if is_windows:
            # Inno setup stuff
            with open("setup.ini", "w") as f:
                min_supported_windows = "6.2.9200" if sys.version_info > (3, 9) else "6.0.6000"
                f.write(f"[Data]\nsource_path={self.buildfolder}\nmin_windows={min_supported_windows}\n")


class AppImageCommand(setuptools.Command):
    description = "build an app image from build output"
    user_options = [
        ("build-folder=", None, "Folder to convert to AppImage."),
        ("dist-file=", None, "AppImage output file."),
        ("app-dir=", None, "Folder to use for packaging."),
        ("app-icon=", None, "The icon to use for the AppImage."),
        ("app-exec=", None, "The application to run inside the image."),
        ("yes", "y", 'Answer "yes" to all questions.'),
    ]
    build_folder: typing.Optional[Path]
    dist_file: typing.Optional[Path]
    app_dir: typing.Optional[Path]
    app_name: str
    app_exec: typing.Optional[Path]
    app_icon: typing.Optional[Path]  # source file
    app_id: str  # lower case name, used for icon and .desktop
    yes: bool

    def write_desktop(self):
        assert self.app_dir, "Invalid app_dir"
        desktop_filename = self.app_dir / f"{self.app_id}.desktop"
        with open(desktop_filename, 'w', encoding="utf-8") as f:
            f.write("\n".join((
                "[Desktop Entry]",
                f'Name={self.app_name}',
                f'Exec={self.app_exec}',
                "Type=Application",
                "Categories=Game",
                f'Icon={self.app_id}',
                ''
            )))
        desktop_filename.chmod(0o755)

    def write_launcher(self, default_exe: Path):
        assert self.app_dir, "Invalid app_dir"
        launcher_filename = self.app_dir / "AppRun"
        with open(launcher_filename, 'w', encoding="utf-8") as f:
            f.write(f"""#!/bin/sh
exe="{default_exe}"
match="${{1#--executable=}}"
if [ "${{#match}}" -lt "${{#1}}" ]; then
    exe="$match"
    shift
elif [ "$1" = "-executable" ] || [ "$1" = "--executable" ]; then
    exe="$2"
    shift; shift
fi
tmp="${{exe#*/}}"
if [ ! "${{#tmp}}" -lt "${{#exe}}" ]; then
    exe="{default_exe.parent}/$exe"
fi
export LD_LIBRARY_PATH="$LD_LIBRARY_PATH:$APPDIR/{default_exe.parent}/lib"
$APPDIR/$exe "$@"
""")
        launcher_filename.chmod(0o755)

    def install_icon(self, src: Path, name: typing.Optional[str] = None, symlink: typing.Optional[Path] = None):
        assert self.app_dir, "Invalid app_dir"
        try:
            from PIL import Image
        except ModuleNotFoundError:
            if not self.yes:
                input("Requirement PIL is not satisfied, press enter to install it")
            subprocess.call([sys.executable, '-m', 'pip', 'install', 'Pillow', '--upgrade'])
            from PIL import Image
        im = Image.open(src)
        res, _ = im.size

        if not name:
            name = src.stem
        ext = src.suffix
        dest_dir = Path(self.app_dir / f'usr/share/icons/hicolor/{res}x{res}/apps')
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_file = dest_dir / f'{name}{ext}'
        shutil.copy(src, dest_file)
        if symlink:
            symlink.symlink_to(dest_file.relative_to(symlink.parent))

    def initialize_options(self):
        self.build_folder = None
        self.app_dir = None
        self.app_name = self.distribution.metadata.name
        self.app_icon = self.distribution.executables[0].icon
        self.app_exec = Path('opt/{app_name}/{exe}'.format(
            app_name=self.distribution.metadata.name, exe=self.distribution.executables[0].target_name
        ))
        self.dist_file = Path("dist", "{app_name}_{app_version}_{platform}.AppImage".format(
            app_name=self.distribution.metadata.name, app_version=self.distribution.metadata.version,
            platform=sysconfig.get_platform()
        ))
        self.yes = False

    def finalize_options(self):
        if not self.app_dir:
            self.app_dir = self.build_folder.parent / "AppDir"
        self.app_id = self.app_name.lower()

    def run(self):
        self.dist_file.parent.mkdir(parents=True, exist_ok=True)
        if self.app_dir.is_dir():
            shutil.rmtree(self.app_dir)
        self.app_dir.mkdir(parents=True)
        opt_dir = self.app_dir / "opt" / self.distribution.metadata.name
        shutil.copytree(self.build_folder, opt_dir)
        root_icon = self.app_dir / f'{self.app_id}{self.app_icon.suffix}'
        self.install_icon(self.app_icon, self.app_id, symlink=root_icon)
        shutil.copy(root_icon, self.app_dir / '.DirIcon')
        self.write_desktop()
        self.write_launcher(self.app_exec)
        print(f'{self.app_dir} -> {self.dist_file}')
        subprocess.call(f'ARCH={build_arch} ./appimagetool -n "{self.app_dir}" "{self.dist_file}"', shell=True)


def find_libs(*args: str) -> typing.Sequence[typing.Tuple[str, str]]:
    """Try to find system libraries to be included."""
    if not args:
        return []

    arch = build_arch.replace('_', '-')
    libc = 'libc6'  # we currently don't support musl

    def parse(line):
        lib, path = line.strip().split(' => ')
        lib, typ = lib.split(' ', 1)
        for test_arch in ('x86-64', 'i386', 'aarch64'):
            if test_arch in typ:
                lib_arch = test_arch
                break
        else:
            lib_arch = ''
        for test_libc in ('libc6',):
            if test_libc in typ:
                lib_libc = test_libc
                break
        else:
            lib_libc = ''
        return (lib, lib_arch, lib_libc), path

    if not hasattr(find_libs, "cache"):
        ldconfig = shutil.which("ldconfig")
        assert ldconfig, "Make sure ldconfig is in PATH"
        data = subprocess.run([ldconfig, "-p"], capture_output=True, text=True).stdout.split("\n")[1:]
        find_libs.cache = {  # type: ignore [attr-defined]
            k: v for k, v in (parse(line) for line in data if "=>" in line)
        }

    def find_lib(lib, arch, libc):
        for k, v in find_libs.cache.items():
            if k == (lib, arch, libc):
                return v
        for k, v, in find_libs.cache.items():
            if k[0].startswith(lib) and k[1] == arch and k[2] == libc:
                return v
        return None

    res = []
    for arg in args:
        # try exact match, empty libc, empty arch, empty arch and libc
        file = find_lib(arg, arch, libc)
        file = file or find_lib(arg, arch, '')
        file = file or find_lib(arg, '', libc)
        file = file or find_lib(arg, '', '')
        # resolve symlinks
        for n in range(0, 5):
            res.append((file, os.path.join('lib', os.path.basename(file))))
            if not os.path.islink(file):
                break
            dirname = os.path.dirname(file)
            file = os.readlink(file)
            if not os.path.isabs(file):
                file = os.path.join(dirname, file)
    return res


cx_Freeze.setup(
    name="Archipelago Stardew Valley 1.5.x Tracker",
    version=f"1.0.1",
    description="Archipelago Stardew Valley 1.5.x Tracker",
    executables=exes,
    options={
        "build_exe": {
            "packages": ["worlds", "websockets"],
            "includes": [],
            "excludes": ["kivy", "cymem", "numpy", "Cython", "PySide2", "PIL",
                         "pandas"],
            "zip_include_packages": ["*"],
            "zip_exclude_packages": ["worlds", "sc2", "orjson"],  # TODO: remove orjson here once we drop py3.8 support
            "include_files": [("stardew_tracker_options.yaml", "stardew_tracker_options.yaml")],  # broken in cx 6.14.0, we use more special sauce now
            "include_msvcr": False,
            "replace_paths": ["*."],
            "optimize": 1,
            "build_exe": buildfolder,
            "extra_data": extra_data,
            "extra_libs": extra_libs,
            "bin_includes": ["libffi.so", "libcrypt.so"] if is_linux else []
        },
        "bdist_appimage": {
           "build_folder": buildfolder,
        },
    },
    # override commands to get custom stuff in
    cmdclass={
        "build": BuildCommand,
        "build_exe": BuildExeCommand,
        "bdist_appimage": AppImageCommand,
    },
)
