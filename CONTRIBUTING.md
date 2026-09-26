# Contributing to Annealage Pod

Thanks for your interest. Read the licensing section before submitting a patch; it sets out the grant you make to the project by submitting.

## Commit messages

Subjects follow the MicroPython convention:

```
<scope>: <Capitalised subject ending with period.>

Optional body wrapped at 75 characters per line.

Signed-off-by: Your Name <you@example.com>
```

Scope is a short path prefix (`usbhost`, `usbip`, `annealage_pod/boot`, `docs/esp32-s3/runbook`, etc.). Subjects are at most 72 characters and end with a full stop.

A `pre-commit` hook enforces this. After cloning:

```
pip install pre-commit
pre-commit install --hook-type commit-msg
```

The hook calls `tools/verifygitlog.py --check-file` on each commit message.

## Pull requests

- Rebase onto current `main` before submitting; do not merge `main` into your branch.
- One logical change per pull request. Small focused PRs are easier to review and revert.
- Include a test or a reproducer in the same PR where it is reasonable to do so.
- Pass `pre-commit run --all-files` locally before pushing.

## Contribution licensing

Annealage Pod is AGPL-3.0-only, with an extra linking permission on the firmware, and I also offer commercial licences. For contributed code to be included in those, I need the grant below from every contributor. By submitting a contribution (pull request, patch or any other change) you agree to the following.

### Developer Certificate of Origin

You certify the contribution under the Developer Certificate of Origin 1.1 (<https://developercertificate.org/>). Sign off each commit with:

```
Signed-off-by: Your Name <your.email@example.com>
```

(`git commit -s` adds this.) The sign-off certifies you wrote the contribution or otherwise have the right to submit it under these terms.

### Licence grant

You grant Andrew Leech a perpetual, worldwide, irrevocable, royalty-free, sublicensable, and transferable licence to use, reproduce, modify, distribute, and relicense your contribution, in whole or in part, under any terms, including the AGPL and any commercial licence Andrew Leech offers, now or in future.

You confirm you have the right to grant this licence, ie. the contribution is your own work or you're authorised to submit it under these terms.

### Outbound licence

Your contribution is also available to everyone under `AGPL-3.0-only`, plus the firmware linking permission for anything under `src/boards/`, `src/c_modules/` and `src/mpy/`. You keep copyright in your work.

### Third-party code

Code taken from another project keeps that project's licence. Changes to the CMSIS-DAP sources in `src/c_modules/dapprobe/vendor/cmsis-dap/`, or the port files derived from them, are contributed under Apache-2.0 and must keep ARM's notices.

If you bring in code from elsewhere, keep its notices, give the file an SPDX header (or a `REUSE.toml` annotation if it can't carry one) and check `uvx reuse lint` passes. Don't add code from an upstream that has no licence.

### Other terms

If you can't grant the licence above, eg. your employer owns the work and hasn't authorised it, or you need a separate contributor agreement, email andrew@alelec.net before submitting.
