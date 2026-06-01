@echo off
echo ============================================
echo  Pallia Trans Signing Helper — Build Script
echo ============================================
echo.

echo Installing dependencies...
pip install -r requirements.txt
pip install pyinstaller

echo.
echo Building PalliaSignHelper.exe ...
pyinstaller ^
  --onefile ^
  --name PalliaSignHelper ^
  --hidden-import pyhanko.sign.pkcs11 ^
  --hidden-import pyhanko.sign.signers ^
  --hidden-import pyhanko.sign.signers.pdf_signer ^
  --hidden-import pyhanko.pdf_utils.incremental_writer ^
  --hidden-import pyhanko.pdf_utils.images ^
  --hidden-import pyhanko.stamp ^
  --hidden-import pkcs11 ^
  --hidden-import asn1crypto ^
  --hidden-import asn1crypto.x509 ^
  --hidden-import loguru ^
  --collect-all pyhanko ^
  main.py

echo.
if exist dist\PalliaSignHelper.exe (
  echo SUCCESS — dist\PalliaSignHelper.exe is ready.
  echo Copy it to any Windows PC that has the DSC token.
) else (
  echo FAILED — check errors above.
)
echo.
pause
