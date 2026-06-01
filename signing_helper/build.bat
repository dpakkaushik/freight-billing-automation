@echo off
echo ============================================
echo  Pallia Trans Signing Helper — Build Script
echo ============================================

pip install -r requirements.txt
pip install pyinstaller

pyinstaller ^
  --onefile ^
  --noconsole ^
  --name PalliaSignHelper ^
  --hidden-import pystray._win32 ^
  --hidden-import win32api ^
  --hidden-import win32con ^
  --hidden-import win32gui ^
  --hidden-import winreg ^
  --hidden-import pyhanko.sign.pkcs11 ^
  --hidden-import pyhanko.sign.signers ^
  --hidden-import pyhanko.sign.signers.pdf_signer ^
  --hidden-import pyhanko.pdf_utils.incremental_writer ^
  --hidden-import pyhanko.pdf_utils.images ^
  --hidden-import pyhanko.stamp ^
  --hidden-import pkcs11 ^
  --hidden-import asn1crypto ^
  --collect-all pyhanko ^
  --collect-all pystray ^
  main.py

if exist dist\PalliaSignHelper.exe (
  echo.
  echo SUCCESS — dist\PalliaSignHelper.exe is ready.
) else (
  echo.
  echo FAILED — check errors above.
)
pause
