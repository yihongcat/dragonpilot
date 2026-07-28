# Project workflow

For any change that may be pulled to an openpilot device or tested in a car:

1. Read and follow `PRE_DRIVE_TESTING.md`.
2. Make all source-code changes in the local workspace. Do not edit source files directly on the device.
3. Keep `test/no-driver-cam-fix` separate from `merge/comma-master` unless the user explicitly requests a merge.
4. Run the applicable local pre-drive gates before recommending an in-car test.
5. Report every passed, failed, and skipped gate. Do not describe a partial run as a full pass.
6. Device-side work is limited to reading diagnostics and, when explicitly requested, pulling an already-pushed branch and verifying its build/runtime state.
7. Prefer `scripts/pre_drive_docker.ps1` for repeatable local gates on Windows. Fall back to the WSL script only when Docker is unavailable.
8. Docker gates test a clean local commit in an isolated Linux workspace. Commit the intended local snapshot before invoking them; do not push it automatically.
