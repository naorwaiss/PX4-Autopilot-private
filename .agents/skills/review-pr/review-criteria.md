# PX4 review criteria

Shared by the interactive `review-pr` skill and the CI review in
`Tools/ci/ai_review/`. Edit the criteria here, not in either consumer.

Never judge a hunk from the diff alone. Read the enclosing function and file, and grep for callers and consumers — a PR is always reviewed in the context of the surrounding code.

- **Merit and need.** Is this solving a real problem or papering over one? Trace the failure mechanism; check the fix addresses the root cause at the right layer. Ask whether a simpler change (different default, existing parameter, deleting code) would achieve the same.
- **Physics and math.** If the change involves physics, estimation, control, or nontrivial math, do a first-principles analysis: re-derive the result and check the implementation against your derivation, not against the PR description. Verify units, reference frames (FRD/NED, body/earth), signs, singularities and edge cases (division by zero, gimbal lock, low airspeed), timestep/discretization dependence, and numerical robustness in the given representation (NaN propagation, cancellation, float32 precision limits).
- **Architecture and maintainability.** Does the change follow the patterns of the code around it (uORB usage, scheduling, param handling, data ownership)? Does it reimplement something an existing library provides (AlphaFilter, SlewRate, mathlib)? Does the logic live in the module that owns the data? Watch for hidden coupling — a change that silently breaks an assumption in another module; if cross-module coupling is genuinely necessary, expect a unit test that codifies the new contract and fails if it is later broken. Embedded reality: flash is scarce, no dynamic allocation in flight code, mind CPU cost.
- **Alternatives.** Identify plausible alternative implementations and judge each: better, worse, or not worth mentioning. Raise only the ones the author should genuinely consider, and say why.
- **Correctness and reliability.** Edge cases, overflow, unsigned arithmetic, initialization, bounds, races, resource exhaustion, and failure-path handling. Flag real defects, not style.
- **Compatibility.** Changes to `msg/`, params, or MAVLink: impact on QGC, uLog tooling, and third-party integrations. uORB: `timestamp` vs `timestamp_sample`, `device_id`. Every new parameter is configuration burden on users — challenge it.
- **New board** (`boards/<mfr>/<board>/`): require a logs.px4.io flight log for the vehicle type, a docs page in `docs/en/flight_controller/`, the manufacturer's own USB VID/PID, a unique board_id in `boards.json`, CI build coverage, flash fit, and no copy-pasted template leftovers or board-local forks of common drivers.

## Contribution requirements

From `CONTRIBUTING.md`, `docs/en/contribute/code.md` and the PR template. Check each one; a gap is a finding about the PR as a whole, with a concrete ask.

- **Test evidence** (`CONTRIBUTING.md`, "Test your changes"). New features need unit or SITL integration tests where practical. Bug fixes need a regression test, or, when that is not feasible, a flight or bench log demonstrating the fix plus reproduction steps for the original bug. Hardware-dependent changes need bench or flight evidence. Reviewers verify that tests or test evidence exist before approving. Missing evidence is a concern; on flight-critical behavior (control, estimation, control allocation, failsafe and arming, navigation, actuator output) with no evidence of any kind it is a blocker. Name the test to add or the log to attach and what it must show.
- **Description matches the change.** The PR states the problem it solves (linked issue, `Fixes #N`), the solution, alternatives considered and test coverage, with links to flight logs. Every behavior change and every affected vehicle type is stated; a description that understates scope is a finding.
- **Users who update.** New or changed parameters, defaults, uORB messages, MAVLink and logged topics, and any change in how a tuned vehicle responds need an upgrade note. User-visible changes need a `docs/en` update or a changelog entry.
- **AI assistance** is disclosed with an `Assisted-by:` commit trailer when used. Only flag a missing trailer when the PR itself says AI was used.

Also, briefly: the PR title must be `type(scope): description` (it becomes the commit subject on squash); note CI status and read failure logs only when plausibly related to the change; do not repeat feedback other reviewers already gave — build on it or stay silent.
