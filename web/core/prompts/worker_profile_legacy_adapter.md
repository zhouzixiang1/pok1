<legacy_adapter_profile>
This profile exists only for archived Botzone/adapter regression.
The formal entrypoint is `main.py`: read Botzone JSON from stdin and emit exactly one JSON
`response` object on stdout. Keep diagnostics on stderr, use `response > 0` as
raise-to-total, and do not claim this profile is a national-native submission.

<profile_verification>
1. `python -m py_compile bots/national_v{version}/*.py`
2. `(cd bots/national_v{version} && python -B -c "import importlib; [importlib.import_module(m) for m in ('main','strategy','postflop','opponent','state') if __import__('pathlib').Path(m + '.py').exists()]")`
3. `python web/core/smoke_tester.py bots/national_v{version}/main.py`
4. Verify stdout remains a single JSON response and diagnostics remain stderr.
</profile_verification>
</legacy_adapter_profile>
