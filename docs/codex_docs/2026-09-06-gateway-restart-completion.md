# Gateway restart repair completion

The listener on port 17601 was PID 169692, started at 20:48:32 before
the local authority commit a4eea38 at 21:49:50. Live settings lacked authority
mode fields. A second gateway failed to bind while the frontend kept running.

Replaced the verified old gateway process tree. The new live listener (PID
135144) returned the original tess-gui-quick-run project, authority_mode=local,
and same-user local filesystem security level. Local authority load succeeded
with an empty record, ready for its first seal. No EDA files were removed.

The dev launcher now propagates backend spawn/exit failures, stops owned Windows
child process trees, and forwards the selected gateway port to Vite. Research
was not resumed automatically; end-to-end baseline completion remains unverified.

Verification: node --test athena-gui/scripts/dev.test.cjs passed (1 regression
test); node --check athena-gui/scripts/dev.cjs and git diff --check passed.
The regression timeout allows Python startup and Windows process-tree cleanup.
