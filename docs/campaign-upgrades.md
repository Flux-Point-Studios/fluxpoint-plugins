# Recording a campaign across a plugin upgrade

Compile checks compare each effective contract with the fields read by its
emitted guards. An old repository copy missing `RedTeamV1.coverage`,
`SliceV1.packetConflict` or `TreeCheckV1.root` fails before any agent starts.
The contracts summary also lists properties omitted from shipped schemas.
Update the repository copies while preserving their additional constraints.

New specification locks carry `lockFormat: 2`. Existing locks remain readable.
If a pre-1.44 lock lacks a references map, the diagnostic names that format gap
and the archive paths to review. It does not claim those files were edited.
Review them and re-lock before compiling new work.

An in-flight legacy run can still be recorded when its exact packet, proof
baseline and obligation identities match. Its artifact and Evidence retain
`LEGACY reference files not covered`; historical archive coverage cannot be
reconstructed from an identity that never recorded it. Modern locks with changed
references still refuse. This compatibility does not change the run summary's
claimed identity or establish modern provenance for old execution.

If old launch arguments name an unpinned absolute input, explicitly acknowledge
that path when recording:

```bash
python plugins/fluxpoint/scripts/record-run.py --run-id legacy-run \
  --result result.json --unpinned-ok /srv/notes/plan.txt
```

Repeat `--unpinned-ok` for each intended path. The allowance is retained in the
artifact and does not waive hash checks on inputs that do carry pins. New
campaigns should pin evidence through the structured resume launcher.
