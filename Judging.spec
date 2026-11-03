from pathlib import Path
import os
from PyInstaller.utils.hooks import collect_all, copy_metadata

root = Path(SPECPATH)
if (root / '.build-venv/tcl/tcl8.6').exists():
    os.environ['TCL_LIBRARY'] = str(root / '.build-venv/tcl/tcl8.6')
    os.environ['TK_LIBRARY'] = str(root / '.build-venv/tcl/tk8.6')
datas, binaries, hiddenimports = collect_all('streamlit')
datas += copy_metadata('streamlit', recursive=True)
datas += [(str(root / 'main.py'), '.'), (str(root / 'config.json'), '.')]
hiddenimports = [m for m in hiddenimports if not m.startswith('streamlit.testing')]
hiddenimports += ['qrcode', 'qrcode.image.pil', 'PIL', 'tkinter', 'tkinter.messagebox', 'tkinter.simpledialog']
a = Analysis([str(root / 'launcher.py')], pathex=[str(root)], binaries=binaries, datas=datas,
             hiddenimports=hiddenimports, hookspath=[], hooksconfig={}, runtime_hooks=[],
             excludes=['streamlit.testing'], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='Judging', debug=False,
          bootloader_ignore_signals=False, strip=False, upx=False, console=False)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='Judging')
