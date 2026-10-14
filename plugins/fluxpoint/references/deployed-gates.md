# Gates on deployed Python pins

A passing suite certifies the dependency versions it ran against. Declare a
deployed lane when production installs a different resolution from development:

```json
{
  "version": 1,
  "gates": {
    "api-deployed": ".venv-deployed/bin/python -m pytest api/tests"
  },
  "deployed": {
    "api": {
      "pins": "api/requirements.txt",
      "python": ".venv-deployed/bin/python",
      "gate": "api-deployed"
    }
  }
}
```

Create this environment using the production installation procedure before
declaring it. The plugin validates it, and does not install or alter packages.
The pins file must exist inside the repository, the interpreter must exist,
and the gate must invoke that interpreter directly. This last rule prevents a
probe of one environment from certifying a suite run in another. A leading
`cd directory &&` is supported; the interpreter path in the lane remains
relative to the repository. On Windows use the environment's `Scripts/python.exe`.

Start the lane as a background task with
`bash "$ROOT/scripts/py.sh" attest.py --run api-deployed --nonce <launch-nonce>`,
then collect it with `attest.py --await <token>`. Before starting the suite,
the wrapper asks the declared interpreter's `importlib.metadata` for each
installed version and compares it with the exact `package==version` pins.
Missing packages, mismatches, empty files, ranges, includes and environment
markers produce a red attestation without starting the suite. This initial
reader supports exact pip pins (including extras), blank lines and comments;
use a resolved lane-specific pins file for other requirement forms.

The row records `pinsSha256` and installed `dependencies` per deployed package.
A `prove:` citation for a deployed gate requires this wrapper witness with a
successful resolution check and unchanged pin hashes. A hook observation
alone does not prove the resolution. Every irreversible graph effect requires
each declared deployed gate's independent, red-halting proof before it. These
long suites run through `--run`/`--await`, outside the Stop hook's time budget.
