{ lib, python3Packages }:

python3Packages.buildPythonApplication {
  pname = "seriallog";
  version = "0.1.0";
  pyproject = true;

  src = lib.cleanSource ../.;

  build-system = [ python3Packages.setuptools ];

  # Standard library only. The tests include the daemon against a fake
  # ser2net on 127.0.0.1, which the build sandbox allows.
  doCheck = true;
  checkPhase = ''
    runHook preCheck
    PYTHONPATH=src:$PYTHONPATH python -m unittest discover -s tests -v
    runHook postCheck
  '';
  pythonImportsCheck = [ "seriallog.cli" ];

  meta = {
    description = "Timestamped logs of serial ports shared by ser2net, with search and alerts";
    license = lib.licenses.mit;
    mainProgram = "seriallog";
    platforms = lib.platforms.linux;
  };
}
